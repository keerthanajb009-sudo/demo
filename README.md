# AI Data Translation Platform MVP

## 1) System Architecture (End-to-End Diagram in Text)
```
[User/Web App]
   | Upload (PDFs, scans, emails)
   v
[FastAPI Backend]
   |--> [Azure Form Recognizer / Document Intelligence]
   |        (OCR, table detection, key-value extraction only)
   |
   |--> [OpenAI API]
   |        (Reasoning + mapping to fixed schema only)
   |
   |--> [Validation Layer]
   |        (Schema enforcement + reconciliation)
   |
   |--> [SQL/Lakehouse]
   |        (Single source of truth)
   |
   |--> [Power BI Embedded]
            (Reads ONLY from SQL/Lakehouse)
```

**Layer responsibilities:**
- **Web App**: Upload documents and display embedded dashboards. No direct access to OpenAI or Form Recognizer.
- **FastAPI Backend**: Orchestration, secure API calls, validation, and data loading into SQL.
- **Azure Form Recognizer**: OCR + extraction only. No business logic.
- **OpenAI API**: Reasoning, mapping, conflict resolution, JSON output only.
- **Validation Layer**: Enforces fixed schema, data types, and reconciliation.
- **SQL/Lakehouse**: Centralized analytics source for Power BI and downstream reporting.
- **Power BI Embedded**: Reads from SQL/Lakehouse only; dashboard in app.

## 2) Data Flow (Step-by-Step)
1. **Document upload** → User uploads file to `/upload-document`.
2. **OCR extraction** → Backend sends file to Azure Form Recognizer.
3. **AI reasoning** → Backend sends OCR JSON to OpenAI with a fixed schema prompt.
4. **Validation** → Pydantic schema validates strict JSON response and reconciles totals.
5. **Database load** → Backend writes to SQL tables (`dim_vendor`, `dim_date`, `fact_invoices`, `fact_invoice_lines`).
6. **Dashboard refresh** → Power BI reads from SQL/Lakehouse and refreshes embedded report.

## 3) Database Schema (Power BI Ready)
Schema file: `sql/schema.sql`

### fact_invoices
- **invoice_id** (TEXT, PK)
- **vendor_id** (TEXT, FK → dim_vendor.vendor_id)
- **invoice_date_key** (INTEGER, FK → dim_date.date_key)
- **due_date** (DATE)
- **currency_code** (CHAR(3))
- **subtotal_amount** (NUMERIC(12,2))
- **tax_amount** (NUMERIC(12,2))
- **total_amount** (NUMERIC(12,2))
- **created_at** (TIMESTAMP)

### dim_vendor
- **vendor_id** (TEXT, PK)
- **vendor_name** (TEXT)
- **vendor_tax_id** (TEXT)

### dim_date
- **date_key** (INTEGER, PK, YYYYMMDD)
- **full_date** (DATE)
- **year** (INTEGER)
- **month** (INTEGER)
- **day** (INTEGER)

## 4) Agentic AI Prompt (Strict JSON)
```
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
```

## 5) Backend MVP (FastAPI)
File: `backend/main.py`

### Enhancements in this implementation
- **Persistent processing cache** stored on disk (`CACHE_DIR`) so results survive restarts.
- **Upload guardrails** with a configurable size limit (`MAX_UPLOAD_MB`) and input validation.
- **Data normalization** for totals reconciliation, line ordering, and non-negative enforcement.
- **Health endpoint** for operational readiness checks.
- **Document retrieval endpoint** for troubleshooting and downstream integrations.

### Run
```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

### Environment
```
AZURE_DOC_INTEL_ENDPOINT=
AZURE_DOC_INTEL_KEY=
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
DATABASE_URL=postgresql+psycopg2://app:app@localhost:5432/analytics
UPLOAD_DIR=./uploads
CACHE_DIR=./cache
MAX_UPLOAD_MB=15
LOG_LEVEL=INFO
```

### Endpoints
- `POST /upload-document` → Stores file locally and returns `document_id`.
- `POST /process-document` → Runs Form Recognizer, calls OpenAI, validates strict JSON (supports `force: true`).
- `POST /load-analytics` → Loads validated data into SQL.
- `GET /documents/{document_id}` → Returns cached processed output.
- `GET /health` → Basic readiness signal for integrations/monitoring.

## 6) Power BI Embedding

### Embedded Flow (User Doesn’t Need Power BI Account)
1. Backend authenticates with Azure AD using a service principal.
2. Backend generates an **embed token** for a report with `GenerateToken` API.
3. Frontend receives token + report metadata from backend.
4. React app embeds report via Power BI JavaScript SDK.

### Token Generation (Backend Pseudocode)
```python
from azure.identity import ClientSecretCredential
import requests

credential = ClientSecretCredential(tenant_id, client_id, client_secret)
access_token = credential.get_token("https://analysis.windows.net/powerbi/api/.default").token

headers = {"Authorization": f"Bearer {access_token}"}
body = {
  "accessLevel": "View",
  "identities": []
}

response = requests.post(
  f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}/GenerateToken",
  json=body,
  headers=headers,
)
embed_token = response.json()["token"]
```

### React Embed Example
```tsx
import powerbi from "powerbi-client";
import { useEffect, useRef } from "react";

export function EmbeddedReport({ embedUrl, accessToken, reportId }) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const config = {
      type: "report",
      id: reportId,
      embedUrl,
      accessToken,
      tokenType: powerbi.models.TokenType.Embed,
      settings: {
        panes: { filters: { visible: false } },
        navContentPaneEnabled: false
      }
    };
    powerbi.embed(containerRef.current, config);
  }, [embedUrl, accessToken, reportId]);

  return <div ref={containerRef} style={{ height: "720px" }} />;
}
```

## 7) Security & Cost Notes
- **API key separation**: Store Form Recognizer, OpenAI, and Power BI credentials separately in Key Vault.
- **Token handling**: Embed tokens generated server-side only; never expose master keys in frontend.
- **Least privilege**: Service principal scoped to a single workspace/report.
- **Cost controls**:
  - Use `prebuilt-invoice` model only when needed.
  - Cache OCR results and avoid re-processing.
  - Use deterministic OpenAI calls (temperature=0) to reduce retries.
  - Batch inserts to SQL for throughput.

## 8) MVP Assumptions
- Azure environment with Form Recognizer and Power BI Embedded capacity.
- External users authenticate to your app (not Power BI).
- SQL/Lakehouse available and network accessible to Power BI.
- Low-volume MVP with production-ready security and validation.
