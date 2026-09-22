import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { endpoints } from "../api/client";
import { ModelEditor } from "../components/ModelEditor";
import { QueryState } from "../components/ui";

// #296 Phase C — edit an existing deployment in the SAME full-screen editor as
// Deploy (just pre-filled from its current params). Reached from the Models
// drawer's "Open editor".
export function EditModel() {
  const { id } = useParams();
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["deployments"], queryFn: endpoints.deployments });
  const back = () => nav("/models");
  return (
    <QueryState q={q} loading={<div className="loading">Loading…</div>}>
      {(deps) => {
        const dep = deps.find((d) => d.id === id);
        if (!dep) return <div className="empty">Deployment not found. <button className="btn sm ghost" onClick={back}>← back to Deployments</button></div>;
        return <ModelEditor mode="edit" deployment={dep} onCancel={back} onDone={back} />;
      }}
    </QueryState>
  );
}
