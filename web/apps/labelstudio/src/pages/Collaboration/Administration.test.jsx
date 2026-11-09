import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Administration } from "./Administration";

const mockCall = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({}) }));
jest.mock("./api", () => ({ ...jest.requireActual("./api"), useCampaignApi: () => mockCall }));

const policy = { assignment_mode: "manual", required_annotations: 2, blind_mode: true, review_mode: "manual" };

beforeEach(() => {
  mockCall.mockReset();
  mockCall.mockImplementation(async (endpoint) => {
    if (endpoint === "me") return { active_organization: 1 };
    if (endpoint === "campaignTaskList") return { tasks: [{ id: 42, data: { text: "Source" } }], total: 1 };
    if (endpoint === "projectMembers" || endpoint === "memberships") return [];
    return { results: [], count: 0, mail_configured: false };
  });
});

test("administrator queues addresses with selected source tasks and sees mail configuration status", async () => {
  render(<MemoryRouter><Administration project={{ id: 17 }} policy={policy} role="admin" onPolicyChanged={jest.fn()} /></MemoryRouter>);
  await screen.findByLabelText("Assign task 42");
  const submit = screen.getByRole("button", { name: "Invite and pre-assign selected tasks" });
  expect(submit.disabled).toBe(true);
  fireEvent.click(screen.getByLabelText("Assign task 42"));
  fireEvent.change(screen.getByLabelText("Email addresses"), { target: { value: "alice@example.test; bob@example.test" } });
  fireEvent.click(submit);
  await waitFor(() => expect(mockCall).toHaveBeenCalledWith("campaignInvite", { pk: 17 }, {
    emails: ["alice@example.test", "bob@example.test"], task_ids: [42],
  }));
  expect(screen.getByText(/Mail delivery is not configured/)).toBeTruthy();
  await screen.findByText(/Invitations queued/);
});

test("QA has no email invitation or activation controls", async () => {
  render(<MemoryRouter><Administration project={{ id: 17 }} policy={policy} role="qa" onPolicyChanged={jest.fn()} /></MemoryRouter>);
  await screen.findByLabelText("Assign task 42");
  expect(screen.queryByText("Invite annotators by email")).toBeNull();
  expect(mockCall.mock.calls.some(([name]) => name === "campaignInvitations")).toBe(false);
});
