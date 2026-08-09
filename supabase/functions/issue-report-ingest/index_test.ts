import {
  BodyTooLargeError,
  handleIssueReportRequest,
  type IssueReportRpcClient,
  readBoundedBody,
} from "./index.ts";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

Deno.test("bounded body reader cancels after the first over-limit chunk", async () => {
  let pulls = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      pulls += 1;
      controller.enqueue(new Uint8Array(64));
      if (pulls >= 10) controller.close();
    },
  });
  const request = new Request("https://example.test/ingest", {
    method: "POST",
    body,
  });

  try {
    await readBoundedBody(request, 128);
    throw new Error("expected BodyTooLargeError");
  } catch (error) {
    assert(error instanceof BodyTooLargeError, "unexpected error type");
  }
  assert(pulls === 3, "the reader consumed more than max + one chunk");
});

Deno.test(
  "malformed and chunked oversized requests are preflight-limited",
  async () => {
    const calls: string[] = [];
    const rpcClient: IssueReportRpcClient = {
      rpc(name) {
        calls.push(name);
        return Promise.resolve({
          data: [{ disposition: "allowed", retry_after_seconds: 0 }],
          error: null,
        });
      },
    };

    const readEnvironment = (name: string) =>
      name === "ISSUE_IP_HMAC_SECRET" ? "t".repeat(32) : undefined;
    const malformed = new Request("https://example.test/ingest", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "cf-connecting-ip": "203.0.113.10",
      },
      body: "{",
    });
    const malformedResponse = await handleIssueReportRequest(
      malformed,
      rpcClient,
      readEnvironment,
    );
    assert(malformedResponse.status === 400, "malformed JSON was accepted");
    assert(
      calls.join(",") === "preflight_issue_ingest_v1",
      "malformed JSON bypassed or exceeded preflight",
    );

    calls.length = 0;
    let pulls = 0;
    const chunkedBody = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1;
        controller.enqueue(new Uint8Array(64 * 1024));
        if (pulls >= 10) controller.close();
      },
    });
    const oversized = new Request("https://example.test/ingest", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "cf-connecting-ip": "203.0.113.10",
      },
      body: chunkedBody,
    });
    const oversizedResponse = await handleIssueReportRequest(
      oversized,
      rpcClient,
      readEnvironment,
    );
    assert(oversizedResponse.status === 413, "oversized body was accepted");
    assert(
      calls.join(",") === "preflight_issue_ingest_v1",
      "oversized body bypassed or exceeded preflight",
    );
    assert(pulls < 10, "oversized request was fully buffered");
  },
);
