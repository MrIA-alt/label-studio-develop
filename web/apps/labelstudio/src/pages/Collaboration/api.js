import { campaignClass } from "./classes";
import { useCallback } from "react";
import { useAPI } from "../../providers/ApiProvider";

export const STATUS_LABELS = {
  ASSIGNED: "Assigned",
  IN_PROGRESS: "In progress",
  SUBMITTED: "Awaiting review",
  REWORK_REQUIRED: "Correction requested",
  CLOSED: "Accepted",
  SKIPPED: "Skipped",
  EXPIRED: "Expired",
  CANCELLED: "Cancelled",
};

export function errorMessage(response) {
  const body = response?.response ?? response;
  const detail = body?.detail ?? body?.validation_errors ?? response?.error;
  if (typeof detail === "string") return detail;
  if (detail)
    return Object.entries(detail)
      .map(([field, value]) => `${field}: ${Array.isArray(value) ? value.join(", ") : value}`)
      .join("; ");
  return "The request could not be completed. Please try again.";
}

export function useCampaignApi() {
  const { callApi } = useAPI();
  return useCallback(
    async (endpoint, params = {}, body) => {
      const options = { params, body, suppressError: true };
      if (["campaignInvite", "campaignInvitationAction"].includes(endpoint)) {
        const csrf = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/);
        if (csrf) options.headers = { "X-CSRFToken": decodeURIComponent(csrf[1]) };
      }
      const response = await callApi(endpoint, options);
      if (!response || response.error || response.$meta?.status >= 400) {
        const error = new Error(errorMessage(response));
        error.status = response?.$meta?.status;
        throw error;
      }
      return response;
    },
    [callApi],
  );
}

export function downloadJSON(data, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function Status({ value }) {
  return (
    <span className={campaignClass(`campaign-status campaign-status--${value?.toLowerCase()}`)}>
      {STATUS_LABELS[value] ?? value}
    </span>
  );
}

export function Pagination({ offset, count, pageSize = 25, onChange }) {
  return (
    <div className={campaignClass("campaign-pagination")}>
      <button type="button" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - pageSize))}>
        Previous
      </button>
      <span>{count ? `${offset + 1}–${Math.min(offset + pageSize, count)} of ${count}` : "No results"}</span>
      <button type="button" disabled={offset + pageSize >= count} onClick={() => onChange(offset + pageSize)}>
        Next
      </button>
    </div>
  );
}
