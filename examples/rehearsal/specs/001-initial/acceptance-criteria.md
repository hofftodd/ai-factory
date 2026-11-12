# sample-desk: Acceptance Criteria

- AC-1 (FR-1.1, FR-1.3) A user creates a product with limits, sees it in the product list,
  edits its upper limit and the list shows the new limit.
- AC-2 (FR-1.2, FR-3.3) An instrument marked out of service does not appear among the
  instruments offered when recording a result.
- AC-3 (FR-2.1, FR-4.1) From the home page, a user chooses "Receive sample", enters a
  product, batch and date, and the new sample appears first in the sample list as
  "Waiting".
- AC-4 (FR-2.2) Filtering the sample list by status "Failed" shows only failed samples,
  and filtering by a batch number shows only that batch.
- AC-5 (FR-3.1, FR-3.2) Recording a value inside the product's limits marks the sample
  "Passed"; a value outside marks it "Failed".
- AC-6 (FR-3.4) A product's page shows a chart with one point per result and lines at
  both limits, and lists its failed samples.
- AC-7 (FR-2.4) Importing a CSV with 10 valid rows and 2 invalid rows adds 10 samples and
  reports the 2 rejected rows with their reasons.
- AC-8 (NFR-3) The home, sample list, sample, product and instrument pages fit a
  1280-pixel-wide window without scrolling sideways.
- AC-9 (NFR-4) With 5,000 samples stored, the first page of 500 loads within one second.
- AC-10 (Demo data) After a fresh start, the home page shows waiting samples and recent
  failures, and every product page shows a trend chart with results.
- AC-11 (NFR-5) The app opens without a login.
