from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = Path(os.getenv("CACHE_DIR", "./cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB", "15"))

AZURE_DOC_INTEL_ENDPOINT = os.getenv("AZURE_DOC_INTEL_ENDPOINT", "")
AZURE_DOC_INTEL_KEY = os.getenv("AZURE_DOC_INTEL_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg2://app:app@localhost:5432/analytics")

app = FastAPI(title="AI Data Translation MVP")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ai-data-translation")


class InvoiceLine(BaseModel):
    line_number: int
    description: str
    quantity: float
    unit_price: float
    line_amount: float

    @field_validator("quantity", "unit_price", "line_amount")
    @classmethod
    def validate_non_negative(cls, value: float, info: Any) -> float:
        if value < 0:
            raise ValueError(f"{info.field_name} cannot be negative")
        return value


class InvoicePayload(BaseModel):
    invoice_id: str = Field(description="Source invoice identifier")
    vendor_id: str
    vendor_name: str
    vendor_tax_id: str | None
    invoice_date: date
    due_date: date | None
    currency_code: str
    subtotal_amount: float
    tax_amount: float
    total_amount: float
    lines: list[InvoiceLine]

    @field_validator("currency_code")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        if len(value) != 3:
            raise ValueError("currency_code must be ISO 4217")
        return value.upper()

    @field_validator("lines")
    @classmethod
    def validate_unique_lines(cls, lines: list[InvoiceLine]) -> list[InvoiceLine]:
        line_numbers = [line.line_number for line in lines]
        if len(line_numbers) != len(set(line_numbers)):
            raise ValueError("line_number values must be unique")
        return lines

    @field_validator("subtotal_amount", "tax_amount", "total_amount")
    @classmethod
    def validate_amounts(cls, value: float, info: Any) -> float:
        if value < 0:
            raise ValueError(f"{info.field_name} cannot be negative")
        return value

    @field_validator("lines")
    @classmethod
    def normalize_line_order(cls, lines: list[InvoiceLine]) -> list[InvoiceLine]:
        return sorted(lines, key=lambda line: line.line_number)

    @field_validator("invoice_id", "vendor_id", "vendor_name")
    @classmethod
    def normalize_strings(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identifier fields cannot be blank")
        return value.strip()

    @classmethod
    def _reconcile_tax(cls, subtotal: float, tax: float, total: float) -> float:
        if abs((subtotal + tax) - total) > 0.01:
            return total - subtotal
        return tax

    def reconcile_totals(self) -> None:
        object.__setattr__(
            self,
            "tax_amount",
            self._reconcile_tax(self.subtotal_amount, self.tax_amount, self.total_amount),
        )


class ProcessRequest(BaseModel):
    document_id: str
    force: bool = False


class LoadRequest(BaseModel):
    document_id: str


class ProcessResult(BaseModel):
    document_id: str
    invoice: InvoicePayload
    raw_ocr: dict[str, Any]


CACHE: dict[str, ProcessResult] = {}


def get_engine() -> Engine:
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def get_doc_client() -> DocumentIntelligenceClient:
    if not AZURE_DOC_INTEL_ENDPOINT or not AZURE_DOC_INTEL_KEY:
        raise HTTPException(status_code=500, detail="Azure Document Intelligence not configured")
    return DocumentIntelligenceClient(
        endpoint=AZURE_DOC_INTEL_ENDPOINT,
        credential=AzureKeyCredential(AZURE_DOC_INTEL_KEY),
    )


def get_openai_client() -> OpenAI:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OpenAI not configured")
    return OpenAI(api_key=OPENAI_API_KEY)


SYSTEM_PROMPT = """
You are an AI data translation engine. You must map Azure Form Recognizer JSON into the FIXED schema.
Rules:
- Output JSON ONLY with the exact schema provided. No markdown, no extra keys.
- Do NOT invent columns. If a value is missing, use null.
- Resolve conflicts by preferring: totals table > key-value pairs > line items > free text.
- Normalize currency_code to ISO 4217.
- Ensure amounts are numeric and totals reconcile: subtotal + tax = total (within 0.01). If conflict, set tax_amount = total - subtotal.
- Dates must be ISO 8601 (YYYY-MM-DD).
Schema:
{
  "invoice_id": "string",
  "vendor_id": "string",
  "vendor_name": "string",
  "vendor_tax_id": "string|null",
  "invoice_date": "YYYY-MM-DD",
  "due_date": "YYYY-MM-DD|null",
  "currency_code": "string",
  "subtotal_amount": number,
  "tax_amount": number,
  "total_amount": number,
  "lines": [
    {
      "line_number": number,
      "description": "string",
      "quantity": number,
      "unit_price": number,
      "line_amount": number
    }
  ]
}
""".strip()


def cache_path(document_id: str) -> Path:
    return CACHE_DIR / f"{document_id}.json"


def read_cache(document_id: str) -> ProcessResult | None:
    if document_id in CACHE:
        return CACHE[document_id]
    path = cache_path(document_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        cached = ProcessResult.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("Cache read failed for %s: %s", document_id, exc)
        return None
    CACHE[document_id] = cached
    return cached


def write_cache(result_payload: ProcessResult) -> None:
    payload = result_payload.model_dump(mode="json")
    cache_path(result_payload.document_id).write_text(json.dumps(payload, indent=2))


def ensure_upload_within_limit(file_bytes: bytes) -> None:
    max_bytes = int(MAX_UPLOAD_MB * 1024 * 1024)
    if len(file_bytes) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds {MAX_UPLOAD_MB:.1f} MB upload limit",
        )


@app.get("/health")
async def healthcheck() -> dict[str, str]:
    status = "ok"
    if not AZURE_DOC_INTEL_ENDPOINT or not AZURE_DOC_INTEL_KEY or not OPENAI_API_KEY:
        status = "degraded"
    return {
        "status": status,
        "doc_intel_configured": str(bool(AZURE_DOC_INTEL_ENDPOINT and AZURE_DOC_INTEL_KEY)),
        "openai_configured": str(bool(OPENAI_API_KEY)),
        "database_configured": str(bool(DATABASE_URL)),
    }


@app.post("/upload-document")
async def upload_document(file: UploadFile = File(...)) -> dict[str, str]:
    doc_id = str(uuid.uuid4())
    file_bytes = await file.read()
    ensure_upload_within_limit(file_bytes)
    file_path = UPLOAD_DIR / f"{doc_id}_{file.filename}"
    with file_path.open("wb") as f:
        f.write(file_bytes)
    logger.info("Uploaded document %s -> %s", doc_id, file.filename)
    return {"document_id": doc_id, "filename": file.filename}


@app.post("/process-document", response_model=ProcessResult)
async def process_document(payload: ProcessRequest) -> ProcessResult:
    cached = read_cache(payload.document_id)
    if cached and not payload.force:
        logger.info("Returning cached result for %s", payload.document_id)
        return cached

    matching_files = list(UPLOAD_DIR.glob(f"{payload.document_id}_*"))
    if not matching_files:
        raise HTTPException(status_code=404, detail="Document not found")

    doc_path = matching_files[0]
    logger.info("Processing document %s", payload.document_id)
    doc_client = get_doc_client()

    with doc_path.open("rb") as f:
        poller = doc_client.begin_analyze_document("prebuilt-invoice", f)
    result = poller.result()
    ocr_payload = result.as_dict()

    client = get_openai_client()
    response = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(ocr_payload)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )

    raw_json = response.output_text
    try:
        structured = InvoicePayload.model_validate_json(raw_json)
        structured.reconcile_totals()
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result_payload = ProcessResult(
        document_id=payload.document_id,
        invoice=structured,
        raw_ocr=ocr_payload,
    )
    CACHE[payload.document_id] = result_payload
    write_cache(result_payload)
    return result_payload


@app.post("/load-analytics")
async def load_analytics(payload: LoadRequest) -> dict[str, str]:
    cached = read_cache(payload.document_id)
    if not cached:
        raise HTTPException(status_code=404, detail="Document not processed")

    data = cached.invoice
    engine = get_engine()

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO dim_vendor (vendor_id, vendor_name, vendor_tax_id)
                VALUES (:vendor_id, :vendor_name, :vendor_tax_id)
                ON CONFLICT (vendor_id) DO UPDATE SET
                  vendor_name = EXCLUDED.vendor_name,
                  vendor_tax_id = EXCLUDED.vendor_tax_id
                """
            ),
            {
                "vendor_id": data.vendor_id,
                "vendor_name": data.vendor_name,
                "vendor_tax_id": data.vendor_tax_id,
            },
        )

        connection.execute(
            text(
                """
                INSERT INTO dim_date (date_key, full_date)
                VALUES (:date_key, :full_date)
                ON CONFLICT (date_key) DO NOTHING
                """
            ),
            {
                "date_key": int(data.invoice_date.strftime("%Y%m%d")),
                "full_date": data.invoice_date,
            },
        )

        connection.execute(
            text(
                """
                INSERT INTO fact_invoices (
                    invoice_id,
                    vendor_id,
                    invoice_date_key,
                    due_date,
                    currency_code,
                    subtotal_amount,
                    tax_amount,
                    total_amount,
                    created_at
                ) VALUES (
                    :invoice_id,
                    :vendor_id,
                    :invoice_date_key,
                    :due_date,
                    :currency_code,
                    :subtotal_amount,
                    :tax_amount,
                    :total_amount,
                    :created_at
                )
                ON CONFLICT (invoice_id) DO UPDATE SET
                  vendor_id = EXCLUDED.vendor_id,
                  invoice_date_key = EXCLUDED.invoice_date_key,
                  due_date = EXCLUDED.due_date,
                  currency_code = EXCLUDED.currency_code,
                  subtotal_amount = EXCLUDED.subtotal_amount,
                  tax_amount = EXCLUDED.tax_amount,
                  total_amount = EXCLUDED.total_amount
                """
            ),
            {
                "invoice_id": data.invoice_id,
                "vendor_id": data.vendor_id,
                "invoice_date_key": int(data.invoice_date.strftime("%Y%m%d")),
                "due_date": data.due_date,
                "currency_code": data.currency_code,
                "subtotal_amount": data.subtotal_amount,
                "tax_amount": data.tax_amount,
                "total_amount": data.total_amount,
                "created_at": datetime.utcnow(),
            },
        )

        for line in data.lines:
            connection.execute(
                text(
                    """
                    INSERT INTO fact_invoice_lines (
                        invoice_id,
                        line_number,
                        description,
                        quantity,
                        unit_price,
                        line_amount
                    ) VALUES (
                        :invoice_id,
                        :line_number,
                        :description,
                        :quantity,
                        :unit_price,
                        :line_amount
                    )
                    ON CONFLICT (invoice_id, line_number) DO UPDATE SET
                      description = EXCLUDED.description,
                      quantity = EXCLUDED.quantity,
                      unit_price = EXCLUDED.unit_price,
                      line_amount = EXCLUDED.line_amount
                    """
                ),
                {
                    "invoice_id": data.invoice_id,
                    "line_number": line.line_number,
                    "description": line.description,
                    "quantity": line.quantity,
                    "unit_price": line.unit_price,
                    "line_amount": line.line_amount,
                },
            )

    return {"status": "loaded", "document_id": payload.document_id}


@app.get("/documents/{document_id}", response_model=ProcessResult)
async def get_document(document_id: str) -> ProcessResult:
    cached = read_cache(document_id)
    if not cached:
        raise HTTPException(status_code=404, detail="Document not processed")
    return cached
