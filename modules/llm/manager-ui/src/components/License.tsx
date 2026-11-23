// #3299: the model's licence and its Hugging Face model card, shown in the deploy
// dialog BEFORE a deploy — some models carry terms the customer is responsible for.
//
// The licence is what the manager read from the repo (app/model_license.py);
// nothing here infers one from a model name. When the repo could not be read
// (offline box, Hugging Face unreachable) the licence is "unknown", never
// "not stated". The model-card link needs no lookup: it is built from the repo id
// alone and is rendered in every state, offline included (a link, nothing fetched).
//
// CUI-12: licence ids and names are third-party strings — React text only. Links
// open in a new tab with noopener/noreferrer and only when they are https.
import { ApiError, type HfLicense } from "../api/client";

const HF = "https://huggingface.co/";
// same shape the manager accepts (`valid_repo_id`): owner/name, no dot segments
const REPO_RE = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/;

/** The model-card URL for a well-formed repo id, else null. */
export function modelCardUrl(repoId: string | null | undefined): string | null {
  if (!repoId || !REPO_RE.test(repoId)) return null;
  if (repoId.split("/").some((s) => s === "." || s === "..")) return null;
  return HF + repoId;
}

function httpsOnly(url: string | null | undefined): string | null {
  return typeof url === "string" && url.startsWith("https://") ? url : null;
}

function Ext({ href, children, title }: { href: string; children: string; title?: string }) {
  return <a className="origin-link" href={href} target="_blank" rel="noopener noreferrer nofollow" title={title}>{children}</a>;
}

/** Why the licence is unknown, from the failed repo lookup. */
function unknownReason(error: unknown, lic?: HfLicense): string {
  if (lic?.reason === "offline") return "offline";
  if (lic) return "not retrieved";
  if (error instanceof ApiError && error.status === 404) return "repository not found";
  return "offline";
}

export function LicenseText({ license, error, loading }: { license?: HfLicense; error?: unknown; loading?: boolean }) {
  if (loading) return <span className="muted">checking…</span>;
  if (!license || license.state === "unknown") {
    return <span className="muted" title={error instanceof Error ? error.message : undefined}>
      unknown ({unknownReason(error, license)})</span>;
  }
  const url = httpsOnly(license.url);
  if (license.state === "not_stated") {
    return <span>
      <span className="badge warn">License not stated — check the model card</span>
      {url && <> · <Ext href={url} title="a licence file in the repository">LICENSE file in the repo</Ext></>}
    </span>;
  }
  return <span>
    <span className="mono">{license.id}</span>
    {license.name && <span className="muted"> · {license.name}</span>}
    {url ? <> · <Ext href={url} title={license.url_source === "spdx" ? "standard licence text (SPDX)" : "licence text"}>license text</Ext></>
      : <span className="muted"> · no licence text linked — check the model card</span>}
  </span>;
}

/** Licence + model-card lines for the deploy dialog. */
export function DeployLicense({ repoId, license, error, loading }:
  { repoId: string; license?: HfLicense; error?: unknown; loading?: boolean }) {
  const card = httpsOnly(license?.card_url)?.startsWith(HF) ? license!.card_url! : modelCardUrl(repoId);
  return (
    <dl className="kv-list license-block">
      <dt>License</dt><dd className="license-text"><LicenseText license={license} error={error} loading={loading} /></dd>
      <dt>Model card</dt><dd>{card ? <Ext href={card} title="the model card on Hugging Face">{repoId}</Ext>
        : <span className="mono">{repoId}</span>}</dd>
    </dl>
  );
}
