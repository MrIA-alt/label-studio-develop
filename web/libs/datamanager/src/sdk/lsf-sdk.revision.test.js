import { LSFWrapper } from "./lsf-sdk";

jest.mock("@humansignal/ui", () => ({ Button: () => null }));
jest.mock("../utils/feature-flags", () => ({ isFF: () => false }));
jest.mock("@humansignal/core/lib/utils/feature-flags", () => ({ isActive: () => false }));
jest.mock("@humansignal/core", () => ({ imageCache: {} }));
jest.mock("@humansignal/core/lib/utils/annotation-cache", () => ({
  invalidateAnnotationCache: jest.fn(),
  invalidateDistributionCache: jest.fn(),
}));
jest.mock("../components/Common/Modal/Modal", () => ({ Modal: {} }));
jest.mock("./comments-sdk", () => ({ CommentsSdk: class {} }));
jest.mock("./lsf-utils", () => ({ annotationToServer: (value) => value, taskToLSFormat: (value) => value }));
jest.mock("mobx-state-tree", () => ({ isAlive: () => true }));

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

test("a later ignored hydration cannot advance the revision of older content in the editor", async () => {
  const first = deferred();
  const second = deferred();
  const annotation = {
    pk: "11",
    revision: 1,
    areas: { size: 0 },
    versions: { result: [] },
    trackedState: {},
    loadedDate: new Date(),
    leadTime: 0,
    history: { freeze: jest.fn(), safeUnfreeze: jest.fn() },
    deserializeResults(value) {
      this.versions.result = value;
    },
    setRevision(value) {
      this.revision = value;
    },
    serializeAnnotation() {
      return this.versions.result;
    },
    updateObjects: jest.fn(),
    reinitHistory: jest.fn(),
  };
  const wrapper = Object.create(LSFWrapper.prototype);
  wrapper.datamanager = { apiCall: jest.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise) };
  wrapper.task = { annotations: [{ id: 11, revision: 99 }] };
  Object.defineProperty(wrapper, "annotations", { value: [annotation] });
  const a = wrapper._hydrateStubAnnotation(annotation);
  const b = wrapper._hydrateStubAnnotation(annotation);
  const result = [{ type: "choices", value: { choices: ["first"] } }];
  first.resolve({ revision: 3, result });
  await a;
  second.resolve({ revision: 4, result: [{ type: "choices", value: { choices: ["second"] } }] });
  await b;
  const outgoing = wrapper.prepareData(annotation);
  expect(outgoing.result).toEqual(result);
  expect(outgoing.revision).toBe(3);
});

test("conflicts preserve the editor and tell the annotator how to recover", () => {
  const wrapper = Object.create(LSFWrapper.prototype);
  wrapper.datamanager = { invoke: jest.fn() };
  wrapper.showOperationToast(409, "Saved", "Failed", {});
  expect(wrapper.datamanager.invoke).toHaveBeenCalledWith(
    "toast",
    expect.objectContaining({ message: expect.stringContaining("Your edits are still here") }),
  );
});
