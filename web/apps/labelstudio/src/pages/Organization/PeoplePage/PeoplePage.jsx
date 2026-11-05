import { Button } from "@humansignal/ui";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useUpdatePageTitle } from "@humansignal/core";
import { HeidiTips } from "../../../components/HeidiTips/HeidiTips";
import { modal } from "../../../components/Modal/Modal";
import { Space } from "../../../components/Space/Space";
import { cn } from "../../../utils/bem";
import { FF_AUTH_TOKENS, FF_LSDV_E_297, isFF } from "../../../utils/feature-flags";
import { useAPI } from "../../../providers/ApiProvider";
import "./PeopleInvitation.scss";
import { PeopleList } from "./PeopleList";
import "./PeoplePage.scss";
import { TokenSettingsModal } from "@humansignal/app-common/blocks/TokenSettingsModal";
import { IconPlus } from "@humansignal/icons";
import { useToast } from "@humansignal/ui";
import { InviteLink } from "./InviteLink";
import { SelectedUser } from "./SelectedUser";

export const PeoplePage = () => {
  const api = useAPI();
  const apiSettingsModal = useRef();
  const toast = useToast();
  const [selectedUser, setSelectedUser] = useState(null);
  const [invitationOpen, setInvitationOpen] = useState(false);
  const [currentUserRole, setCurrentUserRole] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);

  useUpdatePageTitle("People");

  const isAdmin = currentUserRole === "admin";

  useEffect(() => {
    const fetchRole = async () => {
      try {
        const response = await api.callApi("currentUserRole");
        if (response?.role) {
          setCurrentUserRole(response.role);
        }
      } catch (e) {
        // If endpoint not available, default to no role (read-only)
      }
    };
    fetchRole();
  }, []);

  const handleRoleChange = useCallback(
    async (member, newRole) => {
      try {
        const response = await api.callApi("updateMemberRole", {
          params: { pk: 1, userPk: member.user.id },
          body: { role: newRole },
        });
        if (!response) {
          return;
        }
        toast.show({ message: `Role updated to ${newRole}` });
        // Refresh list so role column updates
        setRefreshKey((k) => k + 1);
        // Update the selected user's role in-place
        if (selectedUser?.id === member.user.id) {
          setSelectedUser((prev) => ({ ...prev, role: newRole }));
        }
      } catch (error) {
        const detail = error?.response?.detail || error?.message || "Failed to update role";
        toast.show({ message: detail, type: "error" });
      }
    },
    [selectedUser],
  );

  const selectUser = useCallback(
    (user) => {
      setSelectedUser(user);
      localStorage.setItem("selectedUser", user?.id);
    },
    [setSelectedUser],
  );

  const apiTokensSettingsModalProps = useMemo(
    () => ({
      title: "API Token Settings",
      style: { width: 480 },
      body: () => (
        <TokenSettingsModal
          onSaved={() => {
            toast.show({ message: "API Token settings saved" });
            apiSettingsModal.current?.close();
          }}
        />
      ),
    }),
    [],
  );

  const showApiTokenSettingsModal = useCallback(() => {
    apiSettingsModal.current = modal(apiTokensSettingsModalProps);
    __lsa("organization.token_settings");
  }, [apiTokensSettingsModalProps]);

  const defaultSelected = useMemo(() => {
    return localStorage.getItem("selectedUser");
  }, []);

  return (
    <div className={cn("people").toClassName()}>
      <div className={cn("people").elem("controls").toClassName()}>
        <Space spread>
          <Space />

          <Space>
            {isFF(FF_AUTH_TOKENS) && (
              <Button look="outlined" onClick={showApiTokenSettingsModal} aria-label="Show API token settings">
                API Tokens Settings
              </Button>
            )}
            <Button
              leading={<IconPlus className="!h-4" />}
              onClick={() => setInvitationOpen(true)}
              aria-label="Invite new member"
            >
              Add Members
            </Button>
          </Space>
        </Space>
      </div>
      <div className={cn("people").elem("content").toClassName()}>
        <PeopleList
          selectedUser={selectedUser}
          defaultSelected={defaultSelected}
          onSelect={(user) => selectUser(user)}
          isAdmin={isAdmin}
          onRoleChange={handleRoleChange}
          refreshKey={refreshKey}
        />

        {selectedUser ? (
          <SelectedUser
            user={selectedUser}
            onClose={() => selectUser(null)}
            isAdmin={isAdmin}
            onRoleChange={handleRoleChange}
          />
        ) : (
          isFF(FF_LSDV_E_297) && <HeidiTips collection="organizationPage" />
        )}
      </div>
      <InviteLink
        opened={invitationOpen}
        onClosed={() => {
          console.log("hidden");
          setInvitationOpen(false);
        }}
      />
    </div>
  );
};

PeoplePage.title = "People";
PeoplePage.path = "/";
