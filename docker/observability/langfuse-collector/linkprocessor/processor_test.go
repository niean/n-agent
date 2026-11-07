package linkprocessor

import (
	"context"
	"encoding/json"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"testing"
)

func TestSanitizeLinksPreservesIdentityAndSpan(t *testing.T) {
	traces := ptrace.NewTraces()
	span := traces.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans().AppendEmpty()
	span.SetName("safe")
	span.Attributes().PutStr("original", "unchanged")
	link := span.Links().AppendEmpty()
	link.SetTraceID(pcommon.TraceID{1, 2, 3})
	link.SetSpanID(pcommon.SpanID{4, 5, 6})
	link.SetFlags(257)
	link.TraceState().FromRaw("vendor=safe")
	link.Attributes().PutStr("langfuse.injection", "sentinel")
	link.Attributes().PutStr("unknown", "sentinel")
	original := ptrace.NewTraces()
	traces.CopyTo(original)
	_, err := sanitize(context.Background(), traces)
	if err != nil {
		t.Fatal(err)
	}
	if link.Attributes().Len() != 0 {
		t.Fatal("link attributes must be empty")
	}
	if link.TraceID() != (pcommon.TraceID{1, 2, 3}) || link.SpanID() != (pcommon.SpanID{4, 5, 6}) || link.Flags() != 257 || link.TraceState().AsRaw() != "vendor=safe" || span.Links().Len() != 1 {
		t.Fatal("link identity changed")
	}
	if span.Name() != "safe" || span.Attributes().AsRaw()["original"] != "unchanged" {
		t.Fatal("unrelated span changed")
	}
	if original.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Links().At(0).Attributes().Len() != 2 {
		t.Fatal("original copy changed")
	}
}

func TestSafeLinksMetadata(t *testing.T) {
	traces := ptrace.NewTraces()
	span := traces.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans().AppendEmpty()
	link := span.Links().AppendEmpty()
	link.SetTraceID(pcommon.TraceID{1})
	link.SetSpanID(pcommon.SpanID{2})
	link.SetFlags(1)
	link.Attributes().PutStr("unknown", "never export")
	_, _ = sanitize(context.Background(), traces)
	value, ok := span.Attributes().Get("langfuse.observation.metadata.links")
	if !ok {
		t.Fatal("missing queryable causal links")
	}
	var links []map[string]any
	if err := json.Unmarshal([]byte(value.Str()), &links); err != nil {
		t.Fatal(err)
	}
	if len(links) != 1 || len(links[0]) != 3 || links[0]["trace_id"] != "01000000000000000000000000000000" || links[0]["span_id"] != "0200000000000000" || links[0]["flags"] != float64(1) {
		t.Fatal("invalid link metadata")
	}
}
