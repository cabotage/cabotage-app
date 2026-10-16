# Observing your application

## Reading application logs

Open **Observe > Logs** on an application to view its output when Loki is configured. Select a process or search for text to narrow the results.

**Structured** display turns JSON objects into rows showing the timestamp, process, level, and message or event. Request details such as method, path, status, and duration appear when the log includes them.

Expand a row to inspect every field, including nested values and request or trace IDs. Open **Original line** inside the expanded row to see the original message, or select **Raw** to show original messages for all rows.

Plain text, malformed JSON, and non-object JSON stay as text in either display. Search matches the original log line in Loki, including fields not shown in the collapsed row.

## Pipeline timing

Open **Observe > Pipeline** to review build and deployment times. Redeploys count only deployment time. Builds that run at the same time count once in the run total, including project, organization, and platform rollup percentiles. The summary and tables still load when charts are unavailable.

## Related

- [Deploying your code](deploying.md)
- [Processes & Procfile](processes.md)
