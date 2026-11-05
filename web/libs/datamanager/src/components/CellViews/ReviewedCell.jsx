const BADGE_STYLES = {
  accepted: {
    background: "#e6f4ea",
    color: "#1e7e34",
    border: "1px solid #b7dfbf",
    label: "Accepted",
  },
  rejected: {
    background: "#fce8e6",
    color: "#c62828",
    border: "1px solid #f5c6c2",
    label: "Rejected",
  },
  false: {
    background: "#f0f0f0",
    color: "#666",
    border: "1px solid #ddd",
    label: "Not Reviewed",
  },
};

export const ReviewedCell = ({ value }) => {
  const status = value || "false";
  const badge = BADGE_STYLES[status] || BADGE_STYLES["false"];

  return (
    <span
      style={{
        display: "inline-block",
        padding: "2px 8px",
        borderRadius: "4px",
        fontSize: "12px",
        fontWeight: 600,
        lineHeight: "18px",
        background: badge.background,
        color: badge.color,
        border: badge.border,
        whiteSpace: "nowrap",
      }}
    >
      {badge.label}
    </span>
  );
};

ReviewedCell.userSelectable = false;

ReviewedCell.style = {
  minWidth: 100,
};
