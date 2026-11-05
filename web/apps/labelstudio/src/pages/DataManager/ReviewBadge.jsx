import { useEffect, useState } from "react";
import { useAPI } from "../../providers/ApiProvider";

const REVIEW_LABELS = {
  pending: "Pending Review",
  accepted: "Accepted",
  rejected: "Rejected",
};

const badgeColors = {
  pending: { background: "#f0f0f0", color: "#666" },
  accepted: { background: "#e6f4ea", color: "#1e7e34" },
  rejected: { background: "#fce8e6", color: "#c62828" },
};

const getTaskIdFromUrl = () => {
  const params = new URLSearchParams(window.location.search);
  return params.get("task");
};

export const ReviewBadge = () => {
  const api = useAPI();
  const [taskId, setTaskId] = useState(getTaskIdFromUrl());
  const [annotations, setAnnotations] = useState([]);

  useEffect(() => {
    const check = () => {
      const newTaskId = getTaskIdFromUrl();
      if (newTaskId !== taskId) setTaskId(newTaskId);
    };
    const interval = setInterval(check, 500);
    return () => clearInterval(interval);
  }, [taskId]);

  useEffect(() => {
    if (!taskId) {
      setAnnotations([]);
      return;
    }
    const fetchData = async () => {
      const response = await api.callApi("annotations", {
        params: { pk: taskId },
      });
      if (Array.isArray(response) && response.length > 0) {
        setAnnotations(response);
      } else {
        setAnnotations([]);
      }
    };
    fetchData();
  }, [taskId]);

  if (!taskId || annotations.length === 0) return null;

  return (
    <div style={{
      position: "fixed",
      bottom: "16px",
      right: "16px",
      width: "340px",
      background: "#fff",
      border: "1px solid #ddd",
      borderRadius: "8px",
      boxShadow: "0 4px 16px rgba(0,0,0,0.15)",
      zIndex: 99999,
    }}>
      <div style={{
        fontWeight: 600,
        fontSize: "14px",
        padding: "10px 16px",
        borderBottom: "1px solid #eee",
        background: "#f8f8f8",
        borderRadius: "8px 8px 0 0",
      }}>
        Review Status — Task #{taskId}
      </div>
      <div style={{ padding: "10px 16px" }}>
        {annotations.map((a) => {
          const status = a.review_status || "pending";
          const colors = badgeColors[status];
          return (
            <div key={a.id} style={{ marginBottom: annotations.length > 1 ? "8px" : 0 }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: a.review_comment ? "4px" : 0 }}>
                <span style={{
                  display: "inline-block",
                  padding: "3px 10px",
                  borderRadius: "4px",
                  fontSize: "12px",
                  fontWeight: 600,
                  background: colors.background,
                  color: colors.color,
                }}>
                  Annotation #{a.id}: {REVIEW_LABELS[status]}
                </span>
                {a.completed_by && (
                  <span style={{ fontSize: "11px", color: "#999" }}>by User #{a.completed_by}</span>
                )}
              </div>
              {a.review_comment && (
                <div style={{
                  fontSize: "12px", color: "#555", padding: "6px 8px",
                  background: "#f5f5f5", borderRadius: "4px", lineHeight: "1.4",
                }}>
                  <strong>Comment:</strong> {a.review_comment}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};
