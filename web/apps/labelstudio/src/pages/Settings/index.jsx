import { useEffect, useState } from "react";
import { SidebarMenu } from "../../components/SidebarMenu/SidebarMenu";
import { useAPI } from "../../providers/ApiProvider";
import { WebhookPage } from "../WebhookPage/WebhookPage";
import { DangerZone } from "./DangerZone";
import { GeneralSettings } from "./GeneralSettings";
import { AnnotationSettings } from "./AnnotationSettings";
import { LabelingSettings } from "./LabelingSettings";
import { MachineLearningSettings } from "./MachineLearningSettings/MachineLearningSettings";
import { MembersSettings } from "./MembersSettings";
import { PredictionsSettings } from "./PredictionsSettings/PredictionsSettings";
import { StorageSettings } from "./StorageSettings/StorageSettings";
import "./settings.scss";

const allMenuItems = [
  GeneralSettings,
  LabelingSettings,
  AnnotationSettings,
  MachineLearningSettings,
  PredictionsSettings,
  StorageSettings,
  MembersSettings,
  WebhookPage,
  DangerZone,
];

const qaMenuItems = [MembersSettings];

export const MenuLayout = ({ children, ...routeProps }) => {
  const api = useAPI();
  const [userRole, setUserRole] = useState(null);

  useEffect(() => {
    const fetchRole = async () => {
      const response = await api.callApi("currentUserRole");
      if (response?.role) setUserRole(response.role);
    };
    fetchRole();
  }, []);

  const menuItems = userRole === "qa" ? qaMenuItems : allMenuItems;

  return (
    <SidebarMenu
      menuItems={menuItems.filter(Boolean)}
      path={routeProps.match.url}
      children={children}
    />
  );
};

const pages = {
  AnnotationSettings,
  LabelingSettings,
  MachineLearningSettings,
  MembersSettings,
  PredictionsSettings,
  StorageSettings,
  WebhookPage,
  DangerZone,
};

export const SettingsPage = {
  title: "Settings",
  path: "/settings",
  exact: true,
  layout: MenuLayout,
  component: GeneralSettings,
  pages,
};
