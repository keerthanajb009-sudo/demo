CREATE TABLE IF NOT EXISTS dim_vendor (
  vendor_id TEXT PRIMARY KEY,
  vendor_name TEXT NOT NULL,
  vendor_tax_id TEXT
);

CREATE TABLE IF NOT EXISTS dim_date (
  date_key INTEGER PRIMARY KEY,
  full_date DATE NOT NULL,
  year INTEGER GENERATED ALWAYS AS (EXTRACT(YEAR FROM full_date)) STORED,
  month INTEGER GENERATED ALWAYS AS (EXTRACT(MONTH FROM full_date)) STORED,
  day INTEGER GENERATED ALWAYS AS (EXTRACT(DAY FROM full_date)) STORED
);

CREATE TABLE IF NOT EXISTS fact_invoices (
  invoice_id TEXT PRIMARY KEY,
  vendor_id TEXT NOT NULL REFERENCES dim_vendor(vendor_id),
  invoice_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
  due_date DATE,
  currency_code CHAR(3) NOT NULL,
  subtotal_amount NUMERIC(12,2) NOT NULL,
  tax_amount NUMERIC(12,2) NOT NULL,
  total_amount NUMERIC(12,2) NOT NULL,
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_invoice_lines (
  invoice_id TEXT NOT NULL REFERENCES fact_invoices(invoice_id),
  line_number INTEGER NOT NULL,
  description TEXT NOT NULL,
  quantity NUMERIC(12,2) NOT NULL,
  unit_price NUMERIC(12,2) NOT NULL,
  line_amount NUMERIC(12,2) NOT NULL,
  PRIMARY KEY (invoice_id, line_number)
);
