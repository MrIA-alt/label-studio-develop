import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { CollaborationPage } from "./Collaboration";

const mockCall = jest.fn();
jest.mock("@humansignal/core/providers/AuthProvider", () => ({
  useAuth: () => ({ user: { id: 1, active_organization: 1 } }),
}));
jest.mock("../../providers/ProjectProvider", () => ({
  useProject: () => ({ project: { id: 17, title: "Private campaign" } }),
}));
jest.mock("./api", () => ({ useCampaignApi: () => mockCall }));
jest.mock("./MyTasks", () => ({ MyTasks: () => <p>Personal queue</p> }));
jest.mock("./Supervision", () => ({ Supervision: () => <p>Supervisor queue</p> }));
jest.mock("./Administration", () => ({
  Administration: () => (
    <>
      <p>Campaign administration</p>
      <input aria-label="Unsaved settings note" />
    </>
  ),
}));

beforeEach(() => mockCall.mockReset());

test("annotators land on their queue without administration or review controls", async () => {
  mockCall.mockImplementation(async (endpoint) =>
    endpoint === "me"
      ? { id: 1, email: "annotator@example.test", active_organization: 1 }
      : endpoint === "currentUserRole"
        ? { role: "labeller" }
        : { blind_mode: true },
  );
  render(
    <MemoryRouter>
      <CollaborationPage />
    </MemoryRouter>,
  );
  await screen.findByText("Personal queue");
  expect(screen.queryByRole("button", { name: "Supervision" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Administration" })).toBeNull();
  expect(mockCall).toHaveBeenCalledWith("campaignPolicy", { pk: 17 });
});

test("QA opens supervision and can reach distribution without global administration", async () => {
  mockCall.mockImplementation(async (endpoint) =>
    endpoint === "me"
      ? { id: 1, email: "qa@example.test", active_organization: 1 }
      : endpoint === "currentUserRole"
        ? { role: "qa" }
        : {},
  );
  render(
    <MemoryRouter>
      <CollaborationPage />
    </MemoryRouter>,
  );
  await screen.findByText("Supervisor queue");
  expect(screen.getByRole("button", { name: "Task distribution" })).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Administration" })).toBeNull();
});

test("failed policy loading is visible and does not render a privileged workspace", async () => {
  mockCall.mockRejectedValue(new Error("Project access denied"));
  render(
    <MemoryRouter>
      <CollaborationPage />
    </MemoryRouter>,
  );
  expect((await screen.findByRole("alert")).textContent).toContain("Project access denied");
  expect(screen.queryByText("Campaign administration")).toBeNull();
});

test("returning to a tab after an account switch removes the previous privileged workspace", async () => {
  let profile = { id: 1, email: "admin@example.test", active_organization: 1 };
  let role = "admin";
  mockCall.mockImplementation(async (endpoint) =>
    endpoint === "me" ? profile : endpoint === "currentUserRole" ? { role } : {},
  );
  render(
    <MemoryRouter>
      <CollaborationPage />
    </MemoryRouter>,
  );
  await screen.findByText("Supervisor queue");
  fireEvent.click(screen.getByRole("button", { name: "Administration" }));
  expect(screen.getByText("Campaign administration")).not.toBeNull();

  profile = { id: 2, email: "alice@example.test", active_organization: 1 };
  role = "labeller";
  fireEvent(window, new Event("blur"));
  fireEvent(window, new Event("focus"));
  expect(screen.queryByRole("button", { name: "Administration" })).toBeNull();

  await screen.findByText("Personal queue");
  expect(screen.queryByRole("button", { name: "Administration" })).toBeNull();
  expect(screen.queryByText("Campaign administration")).toBeNull();
  expect(screen.getByText("Signed in as alice@example.test")).not.toBeNull();
});

test("revalidating the same account preserves unsaved workspace input", async () => {
  mockCall.mockImplementation(async (endpoint) =>
    endpoint === "me"
      ? { id: 1, email: "admin@example.test", active_organization: 1 }
      : endpoint === "currentUserRole"
        ? { role: "admin" }
        : {},
  );
  render(
    <MemoryRouter>
      <CollaborationPage />
    </MemoryRouter>,
  );
  await screen.findByText("Supervisor queue");
  fireEvent.click(screen.getByRole("button", { name: "Administration" }));
  const input = screen.getByRole("textbox", { name: "Unsaved settings note" });
  fireEvent.change(input, { target: { value: "Keep my local work" } });

  fireEvent(window, new Event("blur"));
  fireEvent(window, new Event("focus"));
  await waitFor(() => expect(screen.queryByText("Checking your campaign access…")).toBeNull());

  expect(screen.getByRole("textbox", { name: "Unsaved settings note" })).toBe(input);
  expect(input.value).toBe("Keep my local work");
});

test("a redundant focus without leaving the window keeps verified controls clickable", async () => {
  const profile = { id: 1, email: "admin@example.test", active_organization: 1 };
  mockCall.mockImplementation(async (endpoint) =>
    endpoint === "me" ? profile : endpoint === "currentUserRole" ? { role: "admin" } : {},
  );
  render(
    <MemoryRouter>
      <CollaborationPage />
    </MemoryRouter>,
  );
  await screen.findByText("Supervisor queue");
  const requests = mockCall.mock.calls.length;

  fireEvent(window, new Event("focus"));
  fireEvent.click(screen.getByRole("button", { name: "Administration" }));
  expect(screen.getByText("Campaign administration")).not.toBeNull();
  expect(mockCall).toHaveBeenCalledTimes(requests);
});

test("returning from a hidden tab checks a changed identity before showing its workspace", async () => {
  let profile = { id: 1, email: "admin@example.test", active_organization: 1 };
  let role = "admin";
  mockCall.mockImplementation(async (endpoint) =>
    endpoint === "me" ? profile : endpoint === "currentUserRole" ? { role } : {},
  );
  render(
    <MemoryRouter>
      <CollaborationPage />
    </MemoryRouter>,
  );
  await screen.findByText("Supervisor queue");
  const hidden = Object.getOwnPropertyDescriptor(document, "hidden");
  try {
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    fireEvent(document, new Event("visibilitychange"));
    profile = { id: 2, email: "alice@example.test", active_organization: 1 };
    role = "labeller";

    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    fireEvent(document, new Event("visibilitychange"));
    expect(screen.queryByRole("button", { name: "Administration" })).toBeNull();
    await screen.findByText("Personal queue");
    expect(screen.queryByText("Supervisor queue")).toBeNull();
    expect(screen.getByText("Signed in as alice@example.test")).not.toBeNull();
  } finally {
    if (hidden) Object.defineProperty(document, "hidden", hidden);
    else delete document.hidden;
  }
});

test("a temporary access-check failure hides work and restores the same unsaved input on retry", async () => {
  let unavailable = false;
  mockCall.mockImplementation(async (endpoint) => {
    if (endpoint === "me") return { id: 1, email: "admin@example.test", active_organization: 1 };
    if (endpoint === "currentUserRole") {
      if (unavailable) throw Object.assign(new Error("Temporarily unavailable"), { status: 503 });
      return { role: "admin" };
    }
    return {};
  });
  render(
    <MemoryRouter>
      <CollaborationPage />
    </MemoryRouter>,
  );
  await screen.findByText("Supervisor queue");
  fireEvent.click(screen.getByRole("button", { name: "Administration" }));
  const input = screen.getByLabelText("Unsaved settings note");
  fireEvent.change(input, { target: { value: "Keep work during a connection failure" } });

  unavailable = true;
  fireEvent(window, new Event("blur"));
  fireEvent(window, new Event("focus"));
  expect((await screen.findByRole("alert")).textContent).toContain("Temporarily unavailable");
  expect(screen.queryByRole("textbox", { name: "Unsaved settings note" })).toBeNull();
  expect(screen.getByLabelText("Unsaved settings note")).toBe(input);

  unavailable = false;
  fireEvent.click(screen.getByRole("button", { name: "Retry access check" }));
  await waitFor(() => expect(screen.queryByText("Checking your campaign access…")).toBeNull());
  expect(screen.getByRole("textbox", { name: "Unsaved settings note" })).toBe(input);
  expect(input.value).toBe("Keep work during a connection failure");
});

test("a confirmed permission revocation removes the previous workspace", async () => {
  let denied = false;
  mockCall.mockImplementation(async (endpoint) => {
    if (endpoint === "me") return { id: 1, email: "admin@example.test", active_organization: 1 };
    if (endpoint === "currentUserRole") {
      if (denied) throw Object.assign(new Error("Access revoked"), { status: 403 });
      return { role: "admin" };
    }
    return {};
  });
  render(
    <MemoryRouter>
      <CollaborationPage />
    </MemoryRouter>,
  );
  await screen.findByText("Supervisor queue");
  fireEvent.click(screen.getByRole("button", { name: "Administration" }));
  expect(screen.getByLabelText("Unsaved settings note")).not.toBeNull();

  denied = true;
  fireEvent(window, new Event("blur"));
  fireEvent(window, new Event("focus"));
  await screen.findByRole("alert");
  expect(screen.queryByLabelText("Unsaved settings note")).toBeNull();
  expect(screen.queryByText("Campaign administration")).toBeNull();
});

test("returning to the original account after a failed account switch opens a fresh workspace", async () => {
  let switched = false;
  mockCall.mockImplementation(async (endpoint) => {
    if (endpoint === "me") return { id: switched ? 2 : 1, email: "test@example.test", active_organization: 1 };
    if (endpoint === "currentUserRole") {
      if (switched) throw Object.assign(new Error("Temporarily unavailable"), { status: 503 });
      return { role: "admin" };
    }
    return {};
  });
  render(
    <MemoryRouter>
      <CollaborationPage />
    </MemoryRouter>,
  );
  await screen.findByText("Supervisor queue");
  fireEvent.click(screen.getByRole("button", { name: "Administration" }));
  switched = true;
  fireEvent(window, new Event("blur"));
  fireEvent(window, new Event("focus"));
  await screen.findByRole("alert");
  expect(screen.queryByLabelText("Unsaved settings note")).toBeNull();

  switched = false;
  fireEvent.click(screen.getByRole("button", { name: "Retry access check" }));
  await screen.findByText("Supervisor queue");
  expect(screen.getByRole("button", { name: "Administration" })).not.toBeNull();
});
