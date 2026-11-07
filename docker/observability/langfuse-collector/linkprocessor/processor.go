// Package linkprocessor removes attributes from links on the Langfuse fork only.
package linkprocessor

import (
	"context"
	"encoding/hex"
	"encoding/json"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor"
	"go.opentelemetry.io/collector/processor/processorhelper"
)

// NewFactory declares mutation so Collector's fanout clones before this branch.
func NewFactory() processor.Factory {
	return processor.NewFactory(component.MustNewType("linksanitize"), func() component.Config { return &struct{}{} }, processor.WithTraces(createTraces, component.StabilityLevelAlpha))
}

func createTraces(ctx context.Context, settings processor.Settings, config component.Config, next consumer.Traces) (processor.Traces, error) {
	return processorhelper.NewTraces(ctx, settings, config, next, sanitize, processorhelper.WithCapabilities(consumer.Capabilities{MutatesData: true}))
}

type safeLink struct {
	TraceID string `json:"trace_id"`
	SpanID  string `json:"span_id"`
	Flags   uint32 `json:"flags"`
}

func sanitize(_ context.Context, traces ptrace.Traces) (ptrace.Traces, error) {
	resources := traces.ResourceSpans()
	for r := 0; r < resources.Len(); r++ {
		scopes := resources.At(r).ScopeSpans()
		for s := 0; s < scopes.Len(); s++ {
			spans := scopes.At(s).Spans()
			for i := 0; i < spans.Len(); i++ {
				span := spans.At(i)
				links := span.Links()
				if links.Len() == 0 {
					continue
				}
				safe := make([]safeLink, 0, links.Len())
				for l := 0; l < links.Len(); l++ {
					link := links.At(l)
					link.Attributes().Clear()
					traceID, spanID := link.TraceID(), link.SpanID()
					safe = append(safe, safeLink{hex.EncodeToString(traceID[:]), hex.EncodeToString(spanID[:]), link.Flags()})
				}
				encoded, err := json.Marshal(safe)
				if err != nil {
					return traces, err
				}
				span.Attributes().PutStr("langfuse.observation.metadata.links", string(encoded))
			}
		}
	}
	return traces, nil
}
