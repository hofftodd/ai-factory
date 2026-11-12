# sample-desk: Requirements

## Purpose
sample-desk is a small web application for a quality-control lab: it keeps track of the
samples the lab receives, the instruments that test them, and the test results, so a lab
technician can see at a glance what is waiting, what failed and how a product's results
are trending.

## Users and workflows
Lab technicians use it all day on a desktop browser. Their main workflows:

1. **Receive a sample.** From the home page, choose "Receive sample", enter the product,
   batch number and received date, and see the new sample at the top of the sample list
   with status "Waiting".
2. **Record a test result.** Find a sample (by product, batch or status), open it, choose
   "Record result", pick the instrument, enter the measured value and the date; the sample's
   status becomes "Passed" or "Failed" against the product's limits.
3. **Review a product's trend.** Open a product and see a chart of its measured values over
   time, with the product's limits drawn on it, and the list of failed samples below.
4. **Import a delivery.** Upload a CSV of received samples (product, batch, received date)
   and see how many were added and which rows were rejected and why.

## Functional requirements

### Products and instruments
- FR-1.1 A product has a name, a unit and a lower and upper limit for its measured value.
- FR-1.2 An instrument has a name and a location, and can be marked out of service.
- FR-1.3 Products and instruments can be listed, created, edited and deleted.

### Samples
- FR-2.1 A sample belongs to one product and has a batch number, a received date and a
  status (Waiting, Passed or Failed).
- FR-2.2 Samples are listed newest first, and can be filtered by product, batch number and
  status.
- FR-2.3 A sample can be created, viewed, edited and deleted.
- FR-2.4 Samples can be imported from a CSV file; invalid rows are rejected with a reason
  and the valid rows are still imported.

### Test results
- FR-3.1 A result records the sample, the instrument, the measured value and the date.
- FR-3.2 Recording a result sets the sample's status: Passed when the value is within the
  product's limits, Failed otherwise.
- FR-3.3 An instrument that is out of service cannot be chosen for a new result.
- FR-3.4 A product's page shows a chart of its results over time with its limits, and the
  list of its failed samples.

### Home page
- FR-4.1 The home page shows the number of samples waiting, the samples that failed in the
  last seven days, and buttons for "Receive sample" and "Import delivery".

## Non-functional requirements
- NFR-1 Back end: Python with FastAPI, storing data in PostgreSQL.
- NFR-2 Front end: TypeScript with React, styled with the Mantine component library.
- NFR-3 Every page is usable at a 1280-pixel-wide window without scrolling sideways.
- NFR-4 The sample list shows the first page of 500 samples within one second.
- NFR-5 Single user, no login, in this version.

## Demo data
The app starts with enough data to try every workflow straight away:
- 5 products with realistic names, units and limits (e.g. "Paracetamol 500 mg tablet",
  assay in %, 95–105);
- 4 instruments in two locations, one of them out of service;
- 300 samples over the last 90 days across the products, about 10% failed and 20 still
  waiting;
- a test result for every sample that isn't waiting.
