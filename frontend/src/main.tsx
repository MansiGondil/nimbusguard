import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import "./index.css";
import { AuthProvider, useAuth } from "./auth";
import { LiveProvider } from "./live";
import Layout from "./components/Layout";
import Login from "./pages/Login";
import Executive from "./pages/Executive";
import Operations from "./pages/Operations";
import Devices from "./pages/Devices";
import DeviceDetail from "./pages/DeviceDetail";
import Incidents from "./pages/Incidents";
import Remediation from "./pages/Remediation";
import Topology from "./pages/Topology";
import Simulator from "./pages/Simulator";
import Audit from "./pages/Audit";

function App() {
  const { user, ready, can } = useAuth();
  if (!ready) return null;
  if (!user) return <Login />;
  return (
    <LiveProvider>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Executive />} />
          <Route path="operations" element={<Operations />} />
          <Route path="devices" element={<Devices />} />
          <Route path="devices/:id" element={<DeviceDetail />} />
          <Route path="incidents" element={<Incidents />} />
          <Route path="incidents/:id" element={<Incidents />} />
          <Route path="remediation" element={<Remediation />} />
          <Route path="topology" element={<Topology />} />
          <Route path="simulator" element={<Simulator />} />
          {can("admin") && <Route path="audit" element={<Audit />} />}
          <Route path="*" element={<Navigate to="/" />} />
        </Route>
      </Routes>
    </LiveProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <App />
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
);
