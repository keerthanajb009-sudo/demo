from __future__ import annotations

import json
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

AZURE_DOC_INTEL_ENDPOINT = os.getenv("AZURE_DOC_INTEL_ENDPOINT", "")
AZURE_DOC_INTEL_KEY = os.getenv("AZURE_DOC_INTEL_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg2://app:app@localhost:5432/analytics")

app = FastAPI(title="AI Data Translation MVP")


class InvoiceLine(BaseModel):
    line_number: int
    description: str
    quantity: float
    unit_price: float
    line_amount: float


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


class ProcessRequest(BaseModel):
    document_id: str


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


@app.post("/upload-document")
async def upload_document(file: UploadFile = File(...)) -> dict[str, str]:
    doc_id = str(uuid.uuid4())
    file_path = UPLOAD_DIR / f"{doc_id}_{file.filename}"
    with file_path.open("wb") as f:
        f.write(await file.read())
    return {"document_id": doc_id, "filename": file.filename}


@app.post("/process-document", response_model=ProcessResult)
async def process_document(payload: ProcessRequest) -> ProcessResult:
    matching_files = list(UPLOAD_DIR.glob(f"{payload.document_id}_*"))
    if not matching_files:
        raise HTTPException(status_code=404, detail="Document not found")

    doc_path = matching_files[0]
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
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result_payload = ProcessResult(
        document_id=payload.document_id,
        invoice=structured,
        raw_ocr=ocr_payload,
    )
    CACHE[payload.document_id] = result_payload
    return result_payload


@app.post("/load-analytics")
async def load_analytics(payload: LoadRequest) -> dict[str, str]:
    if payload.document_id not in CACHE:
        raise HTTPException(status_code=404, detail="Document not processed")

    data = CACHE[payload.document_id].invoice
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
