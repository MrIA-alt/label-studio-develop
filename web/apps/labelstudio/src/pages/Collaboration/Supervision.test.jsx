import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Supervision } from "./Supervision";

const mockCall = jest.fn();
jest.mock("./api", () => ({ useCampaignApi: () => mockCall, Pagination: () => null, downloadJSON: jest.fn() }));
jest.mock("./AnnotationCanvas", () => ({ AnnotationCanvas: () => <p>Native annotation canvas</p> }));

const review = {
  task: { id: 42, data: { text: "Document" } },
  anonymous: true,
  annotations: [
    {
      id: 7,
      revision: 4,
      annotator: "Annotateur A",
      result: [],
      review_status: "pending",
      assignment_status: "SUBMITTED",
      reviews: [],
      revisions: [],
    },
  ],
};

beforeEach(() => {
  mockCall.mockReset();
  mockCall.mockImplementation(async (endpoint) => {
    if (endpoint === "campaignReviewQueue")
      return { count: 1, results: [{ task: 42, annotations: 1, pending: 1, agreement: 1 }] };
    if (endpoint === "campaignTaskReview") return review;
    return {};
  });
});

test("a stale review sends the displayed revision and keeps the comparison visible", async () => {
  const implementation = mockCall.getMockImplementation();
  mockCall.mockImplementation(async (endpoint, ...args) => {
    if (endpoint === "campaignReview") throw new Error("The annotation changed. Reload its current revision.");
    return implementation(endpoint, ...args);
  });
  render(<Supervision project={{ id: 17 }} />);
  fireEvent.click(await screen.findByRole("button", { name: "Compare #42" }));
  fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Reload its current revision");
  expect(screen.getByText("Annotation #7 · Version 4")).not.toBeNull();
  expect(mockCall).toHaveBeenCalledWith(
    "campaignReview",
    {},
    { annotation: 7, revision: 4, decision: "ACCEPT", comment: "" },
  );
  mockCall.mockImplementation(async (endpoint, ...args) => {
    if (endpoint === "campaignTaskReview")
      return { ...review, annotations: [{ ...review.annotations[0], revision: 5 }] };
    return implementation(endpoint, ...args);
  });
  fireEvent.click(screen.getByRole("button", { name: "Reload comparison" }));
  await screen.findByText("Annotation #7 · Version 5");
  fireEvent.click(screen.getByRole("button", { name: "Accept" }));
  await waitFor(() =>
    expect(mockCall).toHaveBeenCalledWith(
      "campaignReview",
      {},
      { annotation: 7, revision: 5, decision: "ACCEPT", comment: "" },
    ),
  );
});

test("batch acceptance uses the revision fetched for each selected task", async () => {
  const confirm = jest.spyOn(window, "confirm").mockReturnValue(true);
  try {
    render(<Supervision project={{ id: 17 }} />);
    fireEvent.click(await screen.findByLabelText("Select task 42"));
    fireEvent.click(screen.getByRole("button", { name: "Accept selected tasks" }));
    await waitFor(() =>
      expect(mockCall).toHaveBeenCalledWith(
        "campaignReview",
        {},
        { annotation: 7, revision: 4, decision: "ACCEPT", comment: "" },
      ),
    );
  } finally {
    confirm.mockRestore();
  }
});
