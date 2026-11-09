import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useCampaignApi } from "./api";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));

test("invitation mutations include the current CSRF cookie", async () => {
  document.cookie = "csrftoken=synthetic-csrf; path=/";
  mockCallApi.mockResolvedValue({ id: 1 });
  function Form() {
    const call = useCampaignApi();
    return <button type="button" onClick={() => call("campaignInvite", { pk: 17 }, { emails: ["a@example.test"], task_ids: [42] })}>Invite</button>;
  }
  render(<Form />);
  fireEvent.click(screen.getByText("Invite"));
  await waitFor(() => expect(mockCallApi).toHaveBeenCalledWith("campaignInvite", expect.objectContaining({
    headers: { "X-CSRFToken": "synthetic-csrf" },
  })));
  document.cookie = "csrftoken=; Max-Age=0; path=/";
});
