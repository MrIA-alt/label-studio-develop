import { render, waitFor } from "@testing-library/react";
import { destroy } from "mobx-state-tree";
import "../tags/visual/View";
import "../tags/object/RichText";
import "../tags/control/Choice";
import "../tags/control/Choices";
import AppStore from "../stores/AppStore";
import { AnnotationCanvas } from "../../../../apps/labelstudio/src/pages/Collaboration/AnnotationCanvas";

if (typeof globalThis.structuredClone === "undefined") {
  globalThis.structuredClone = (value) => JSON.parse(JSON.stringify(value));
}

jest.mock("@humansignal/editor", () => ({}));
jest.mock("keymaster", () => {
  const keymaster = () => {};
  keymaster.unbind = () => {};
  keymaster.setScope = () => {};
  return { __esModule: true, default: keymaster };
});

const result = [{ id: "category", from_name: "category", to_name: "text", type: "choices", value: { choices: ["a"] } }];
const project = {
  label_config:
    '<View><Text name="text" value="$text" /><Choices name="category" toName="text"><Choice value="a" /><Choice value="b" /></Choices></View>',
};

beforeEach(() => {
  window.LabelStudio = jest.fn(function NativeEditorBridge(_, options) {
    const store = AppStore.create(
      {
        config: options.config,
        task: { id: options.task.id, data: JSON.stringify(options.task.data) },
        interfaces: options.interfaces,
      },
      { events: { hasEvent: () => false, invoke: jest.fn() }, messages: {}, settings: {} },
    );
    store.initializeStore({});
    this.annotationStore = store.annotationStore;
    this.destroy = () => destroy(store);
    options.onStorageInitialized(this);
  });
});

test.each([false, true])(
  "campaign canvas loads saved native results before review or arbitration (editable=%s)",
  async (editable) => {
    const ready = jest.fn();
    render(
      <AnnotationCanvas
        project={project}
        task={{ id: 12, data: { text: "Alice Example" } }}
        annotation={{ id: 5, result }}
        editable={editable}
        onReady={ready}
      />,
    );
    await waitFor(() => expect(ready).toHaveBeenCalledWith(expect.any(Function)));
    const serialize = ready.mock.calls.find(([value]) => typeof value === "function")[0];
    expect(serialize()).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ from_name: "category", to_name: "text", type: "choices", value: { choices: ["a"] } }),
      ]),
    );
    const selected = window.LabelStudio.mock.instances[0].annotationStore.selected;
    expect(selected.readonly).toBe(!editable);
    expect(selected.history.canUndo).toBe(false);
  },
);
