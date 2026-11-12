# sample-desk: Design

## Overview
Two parts: a Python/FastAPI back end over PostgreSQL that owns the data and the rules, and
a TypeScript/React front end, built with Vite and styled with Mantine, that talks to it
over a JSON REST API. In development the front end's dev server forwards the API paths to
the back end, so the browser sees one address.

## Back end
- **Data:** four tables (products, instruments, samples, results), with foreign keys from
  samples to products and from results to samples and instruments. Status is stored on
  the sample and recalculated whenever a result is recorded.
- **Layers:** a data module (the tables and queries), a service module per area
  (products and instruments, samples, results, import), and the API module, which only
  validates requests and calls the services. No module mixes these jobs.
- **Rules live in the services:** the pass/fail decision (FR-3.2), the out-of-service
  check (FR-3.3) and the CSV validation (FR-2.4).
- **Lists** are filtered and paginated in the database, never in Python.
- **API:** `/products`, `/instruments`, `/samples` (with filter and page parameters),
  `/samples/{id}/results`, `/products/{id}/trend` and `/imports/samples` (CSV upload),
  documented with OpenAPI.

## Front end
- **Layout:** a Mantine app shell with a left navigation (Home, Samples, Products,
  Instruments) and a header with the app name. Pages use Mantine tables, forms, buttons
  and notifications, so the whole app looks consistent.
- **Pages:** Home (the counts and recent failures, FR-4.1), Samples (filterable table,
  "Receive sample" and "Import delivery"), Sample (details, results, "Record result"),
  Products and Product (with the trend chart), Instruments.
- **Chart:** the product trend uses Mantine's charts (built on Recharts): a line of the
  measured values over time with the two limits as reference lines.
- **Labels** are written for lab technicians ("Received", "Batch", "Measured value"),
  never internal field names.

## Running it
`scripts/start.sh` starts PostgreSQL for development if none is running, creates the
schema, loads the demo data into an empty database, starts the back end and the front
end's dev server, and serves the app on the port it is given. The toolchain image
provides every back-end and front-end dependency (there is no network when the agents
work).

## Key decisions
- **Mantine** gives a consistent, accessible look without writing a design system.
- **Filtering and pagination in SQL** keep the 500-row page under a second (NFR-4).
- **One service module per area** keeps any one file small and its job clear.
