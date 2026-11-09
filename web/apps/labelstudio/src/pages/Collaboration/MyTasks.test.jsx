import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route } from "react-router-dom";
import { MyTasks } from "./MyTasks";

const mockCall = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({}) }));
jest.mock("./api", () => ({ ...jest.requireActual("./api"), useCampaignApi: () => mockCall }));

beforeEach(() => {
  mockCall.mockReset();
  mockCall.mockImplementation(async (endpoint) => {
    if (endpoint === "campaignMyAssignments") return { count: 1, results: [{ id: 9, task: 42, status: "ASSIGNED" }] };
    if (endpoint === "campaignMyReviews")
      return {
        count: 1,
        results: [
          {
            annotation: 7,
            task: 42,
            revision: 1,
            decision: "REQUEST_CORRECTION",
            comment: "Please correct the document date",
          },
        ],
      };
    return {};
  });
});

test("loads only the current project and starts an assignment before opening native annotation", async () => {
  render(
    <MemoryRouter>
      <MyTasks project={{ id: 17 }} policy={{ assignment_mode: "manual" }} />
      <Route path="/projects/17/data">
        <p>Native editor</p>
      </Route>
    </MemoryRouter>,
  );
  const open = await screen.findByRole("button", { name: "Annotate" });
  expect(mockCall).toHaveBeenCalledWith("campaignMyReviews", { project: 17, offset: 0, limit: 25 });
  fireEvent.click(open);
  await screen.findByText("Native editor");
  expect(mockCall).toHaveBeenCalledWith("campaignAssignmentAction", { pk: 9, action: "start" }, {});
});

test("status filters are sent to the server and the author's correction reason remains visible", async () => {
  render(
    <MemoryRouter>
      <MyTasks project={{ id: 17 }} policy={{ assignment_mode: "manual" }} />
    </MemoryRouter>,
  );
  await screen.findByText("Please correct the document date");
  fireEvent.change(screen.getByLabelText("Status"), { target: { value: "REWORK_REQUIRED" } });
  await waitFor(() =>
    expect(mockCall).toHaveBeenCalledWith("campaignMyAssignments", {
      project: 17,
      status: "REWORK_REQUIRED",
      offset: 0,
      limit: 25,
    }),
  );
});

test("a failed assignment start leaves the user on the queue with a useful error", async () => {
  mockCall.mockImplementation(async (endpoint) => {
    if (endpoint === "campaignMyAssignments") return { count: 1, results: [{ id: 9, task: 42, status: "ASSIGNED" }] };
    if (endpoint === "campaignMyReviews") return { count: 0, results: [] };
    throw new Error("This assignment has expired");
  });
  render(
    <MemoryRouter>
      <MyTasks project={{ id: 17 }} policy={{ assignment_mode: "manual" }} />
    </MemoryRouter>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Annotate" }));
  expect((await screen.findByRole("alert")).textContent).toContain("expired");
});

test("archived assignments can be consulted without starting work", async () => {
  render(
    <MemoryRouter>
      <MyTasks project={{ id: 17 }} policy={{ assignment_mode: "manual", is_archived: true }} />
      <Route path="/projects/17/data">
        <p>Read-only native editor</p>
      </Route>
    </MemoryRouter>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "View" }));
  await screen.findByText("Read-only native editor");
  expect(mockCall.mock.calls.some(([endpoint]) => endpoint === "campaignAssignmentAction")).toBe(false);
  expect(screen.queryByRole("button", { name: "Release" })).toBeNull();
});
