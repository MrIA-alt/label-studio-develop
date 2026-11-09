import { campaignClass } from "./classes";
import { useEffect, useRef, useState } from "react";

let editorPromise;
const loadEditor = () => (editorPromise ??= import("@humansignal/editor"));

// Reuse the native editor for text, image and multimodal annotation.
export function AnnotationCanvas({ project, task, annotation, editable = false, onChange, onReady }) {
  const root = useRef(null);
  const instance = useRef(null);
  const change = useRef(onChange);
  const ready = useRef(onReady);
  const [error, setError] = useState("");
  change.current = onChange;
  ready.current = onReady;
  const resultKey = JSON.stringify(annotation?.result ?? []);
  const dataKey = JSON.stringify(task.data);

  useEffect(() => {
    let disposed = false;
    let timer;
    setError("");
    loadEditor()
      .then(() => {
        if (disposed || !root.current) return;
        const initial = JSON.stringify(annotation?.result ?? []);
        let previous = initial;
        const canvas = new window.LabelStudio(root.current, {
          config: project.label_config,
          task: { id: task.id, data: task.data, annotations: [], predictions: [] },
          interfaces: editable ? ["basic", "side-column", "edit-history"] : ["basic", "side-column"],
          onStorageInitialized(ls) {
            if (disposed) return;
            const item = ls.annotationStore.addAnnotation({ result: annotation?.result ?? [], userGenerate: true });
            ls.annotationStore.selectAnnotation(item.id);
            item.deserializeResults(annotation?.result ?? []);
            item.reinitHistory();
            item.setReadonly(!editable);
            ready.current?.(() => item.serializeAnnotation());
            if (editable) {
              timer = setInterval(() => {
                if (disposed) return;
                const result = item.serializeAnnotation();
                const serialized = JSON.stringify(result);
                if (serialized !== previous) {
                  previous = serialized;
                  change.current?.(result);
                }
              }, 300);
            }
          },
          onPresignUrlForProject(_, url) {
            if (/^(?:https?:|data:|blob:|\/)/i.test(url)) return url;
            const fileuri = btoa(unescape(encodeURIComponent(url)));
            return `${window.APP_SETTINGS.hostname}/tasks/${task.id}/resolve/?fileuri=${encodeURIComponent(fileuri)}`;
          },
        });
        instance.current = canvas;
      })
      .catch((reason) => {
        if (!disposed) setError(reason.message ?? "Unable to load the annotation editor.");
      });
    return () => {
      disposed = true;
      clearInterval(timer);
      instance.current?.destroy?.();
      instance.current = null;
      ready.current?.(null);
    };
  }, [project.label_config, task.id, annotation?.id, annotation?.revision, resultKey, dataKey, editable]);

  return (
    <div className={campaignClass("campaign-canvas")}>
      {error && <p role="alert">{error}</p>}
      <div ref={root} />
    </div>
  );
}
