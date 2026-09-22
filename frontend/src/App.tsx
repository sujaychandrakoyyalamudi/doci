import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  Bell,
  BookOpen,
  Check,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  CircleCheck,
  FileCheck2,
  FileSearch,
  Files,
  FileText,
  FolderOpen,
  HelpCircle,
  LayoutDashboard,
  LoaderCircle,
  LockKeyhole,
  LogOut,
  Menu,
  Plus,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Upload,
  Workflow,
  X,
} from "lucide-react";
import { initializeApp } from "firebase/app";
import {
  getAuth,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signOut,
} from "firebase/auth";
import type { Auth } from "firebase/auth";
import { api, downloadDocument, setRole, setTokenProvider } from "./api";
import type {
  Actor,
  Case,
  Config,
  Detail,
  Document,
  Event,
  Evidence,
} from "./types";

type Page = "Overview" | "All cases" | "Documents" | "Approvals" | "Activity";
const editable = [
  "draft",
  "rejected",
  "escalated",
  "needs_information",
  "failed",
];
const statusLabel: Record<string, string> = {
  draft: "Draft",
  queued: "Queued",
  processing: "In review",
  awaiting_approval: "Needs approval",
  resolved: "Resolved",
  rejected: "Rejected",
  escalated: "Escalated",
  failed: "Needs attention",
  needs_information: "Needs information",
};
const icons = {
  Overview: LayoutDashboard,
  "All cases": FolderOpen,
  Documents: Files,
  Approvals: ShieldCheck,
  Activity,
};
const date = (value: string) =>
  new Date(
    value.endsWith("Z") || value.includes("+") ? value : `${value}Z`,
  ).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const time = (value: string) =>
  new Date(
    value.endsWith("Z") || value.includes("+") ? value : `${value}Z`,
  ).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const initials = (value: string) =>
  value
    .split(" ")
    .map((x) => x[0])
    .slice(0, 2)
    .join("");
const formatBytes = (value: number) =>
  value > 1024 * 1024
    ? `${(value / 1024 / 1024).toFixed(1)} MB`
    : `${Math.max(1, Math.round(value / 1024))} KB`;

function Badge({ status }: { status: string }) {
  return (
    <span className={`badge ${status}`}>
      <span />
      {statusLabel[status] || status}
    </span>
  );
}
function Button({
  children,
  onClick,
  kind = "",
  disabled = false,
  type = "button",
}: {
  children: ReactNode;
  onClick?: () => void;
  kind?: string;
  disabled?: boolean;
  type?: "button" | "submit";
}) {
  return (
    <button
      type={type}
      className={`button ${kind}`}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}
function Empty({
  title,
  text,
  icon = <FolderOpen size={28} />,
}: {
  title: string;
  text: string;
  icon?: ReactNode;
}) {
  return (
    <div className="empty">
      <div>{icon}</div>
      <h3>{title}</h3>
      <p>{text}</p>
    </div>
  );
}

function Modal({
  title,
  subtitle,
  children,
  close,
}: {
  title: string;
  subtitle: string;
  children: ReactNode;
  close: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className="modal"
      onCancel={close}
      onClick={(e) => {
        if (e.target === ref.current) close();
      }}
    >
      <div className="modal-heading">
        <div>
          <p className="eyebrow">DOCI WORKSPACE</p>
          <h2>{title}</h2>
          <p>{subtitle}</p>
        </div>
        <button
          className="icon-button"
          aria-label="Close dialog"
          onClick={close}
        >
          <X size={20} />
        </button>
      </div>
      {children}
    </dialog>
  );
}

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [auth, setAuth] = useState<Auth | null>(null);
  const [signedIn, setSignedIn] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let unsubscribe: (() => void) | undefined;
    let active = true;
    api<Config>("/config")
      .then((data) => {
        if (!active) return;
        setConfig(data);
        if (data.auth_mode === "demo") {
          setSignedIn(true);
          return;
        }
        if (!data.firebase_api_key)
          throw new Error(
            "Identity Platform is not configured. Set FIREBASE_API_KEY on the backend.",
          );
        const authClient = getAuth(
          initializeApp({
            apiKey: data.firebase_api_key,
            authDomain: data.firebase_auth_domain,
            projectId: data.project_id,
          }),
        );
        setAuth(authClient);
        unsubscribe = onAuthStateChanged(authClient, (user) => {
          setTokenProvider(user ? () => user.getIdToken() : null);
          setSignedIn(!!user);
        });
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
      unsubscribe?.();
    };
  }, []);
  if (error)
    return (
      <div className="boot-screen">
        <div className="brand-mark">d</div>
        <h1>Workspace unavailable</h1>
        <p>{error}</p>
        <Button onClick={() => location.reload()}>Try again</Button>
      </div>
    );
  if (!config)
    return (
      <div className="boot-screen">
        <LoaderCircle className="spin" />
        <p>Opening your workspace…</p>
      </div>
    );
  if (!signedIn) return <Login auth={auth} />;
  return (
    <Workspace
      config={config}
      signOutUser={() => {
        if (auth) void signOut(auth);
      }}
    />
  );
}

function Login({ auth }: { auth: Auth | null }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!auth) return;
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      await signInWithEmailAndPassword(
        auth,
        String(form.get("email")),
        String(form.get("password")),
      );
    } catch {
      setError(
        "Sign-in failed. Check your email and password or contact your administrator.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="brand">
          <span className="brand-mark">d</span>doci
          <span className="brand-dot">.</span>
        </div>
        <p className="eyebrow">CLARITY IN EVERY CASE</p>
        <h1>
          Welcome to your
          <br />
          review workspace.
        </h1>
        <p>Sign in with your organization account.</p>
        <form onSubmit={submit}>
          <label>
            Email address
            <input required name="email" type="email" autoComplete="username" />
          </label>
          <label>
            Password
            <input
              required
              name="password"
              type="password"
              autoComplete="current-password"
            />
          </label>
          {error && (
            <div role="alert" className="error-box">
              {error}
            </div>
          )}
          <Button type="submit" kind="primary" disabled={busy || !auth}>
            {busy ? (
              <LoaderCircle className="spin" size={16} />
            ) : (
              <LockKeyhole size={16} />
            )}
            Sign in
          </Button>
        </form>
        <div className="login-footer">
          <ShieldCheck size={16} /> Secure organization access
        </div>
      </div>
    </div>
  );
}

function Workspace({
  config,
  signOutUser,
}: {
  config: Config;
  signOutUser: () => void;
}) {
  const [page, setPage] = useState<Page>("Overview");
  const [cases, setCases] = useState<Case[]>([]);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [actor, setActor] = useState<Actor | null>(null);
  const [role, changeRole] = useState("analyst");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [newCase, setNewCase] = useState(false);
  const [help, setHelp] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [priority, setPriority] = useState("all");
  const [busy, setBusy] = useState(false);
  const refreshVersion = useRef(0);
  const refresh = useCallback(async () => {
    const version = ++refreshVersion.current;
    try {
      const [c, d, e, a, selectedDetail] = await Promise.all([
        api<Case[]>("/cases"),
        api<Document[]>("/documents"),
        api<Event[]>("/activity"),
        api<Actor>("/me"),
        selected ? api<Detail>(`/cases/${selected}`) : Promise.resolve(null),
      ]);
      if (version !== refreshVersion.current) return;
      setCases(c);
      setDocuments(d);
      setEvents(e);
      setActor(a);
      setDetail(selectedDetail);
      setError("");
    } catch (e) {
      if (version === refreshVersion.current) setError((e as Error).message);
    } finally {
      if (version === refreshVersion.current) setLoading(false);
    }
  }, [selected, role]);
  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 5000);
    return () => {
      clearInterval(id);
      refreshVersion.current++;
    };
  }, [refresh]);
  useEffect(() => {
    if (toast) {
      const id = setTimeout(() => setToast(""), 4500);
      return () => clearTimeout(id);
    }
  }, [toast]);
  function navigate(target: Page) {
    setPage(target);
    setSelected(null);
    setDetail(null);
    setQuery("");
    setFilter("all");
    setMobileNav(false);
  }
  function openCase(id: string) {
    setSelected(id);
    setDetail(null);
  }
  async function perform(work: () => Promise<unknown>, message: string) {
    setBusy(true);
    setError("");
    try {
      await work();
      await refresh();
      setToast(message);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const approvals = cases.filter((c) => c.status === "awaiting_approval");
  const resolved = cases.filter((c) => c.status === "resolved").length;
  const active = cases.filter(
    (c) => !["resolved", "rejected"].includes(c.status),
  ).length;
  let filtered = cases.filter((c) =>
    `${c.title} ${c.reference} ${c.category}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  if (page === "Approvals")
    filtered = filtered.filter((c) => c.status === "awaiting_approval");
  else if (filter !== "all")
    filtered = filtered.filter((c) =>
      filter === "active"
        ? !["resolved", "rejected"].includes(c.status)
        : c.status === filter,
    );
  if (priority !== "all")
    filtered = filtered.filter((c) => c.priority === priority);
  const canCreate =
    actor && ["analyst", "submitter", "admin"].includes(actor.role);

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileNav ? "mobile-open" : ""}`}>
        <div className="brand">
          <span className="brand-mark">d</span>doci
          <span className="brand-dot">.</span>
        </div>
        <div className="workspace-switch">
          <div className="workspace-icon">
            <Workflow size={17} />
          </div>
          <div>
            <strong>Review workspace</strong>
            <span>
              {config.auth_mode === "demo"
                ? "Demonstration team"
                : "Organization workspace"}
            </span>
          </div>
          <ChevronDown size={14} />
        </div>
        <p className="nav-label">WORKSPACE</p>
        <nav>
          {(Object.keys(icons) as Page[]).map((item) => {
            const Icon = icons[item];
            return (
              <button
                key={item}
                className={`nav-item ${page === item ? "active" : ""}`}
                onClick={() => navigate(item)}
              >
                <Icon size={18} />
                <span>{item}</span>
                {item === "Approvals" && approvals.length > 0 && (
                  <b>{approvals.length}</b>
                )}
              </button>
            );
          })}
        </nav>
        <div className="sidebar-bottom">
          <div className="guardrail">
            <div>
              <ShieldCheck size={18} />
              <span>
                Human judgment.
                <br />
                <strong>At every decision.</strong>
              </span>
            </div>
            <p>
              Agents find the evidence.
              <br />
              Your team makes the call.
            </p>
          </div>
          <button
            className="nav-item help-button"
            onClick={() => setHelp(true)}
          >
            <HelpCircle size={18} />
            <span>Workspace guide</span>
            <ArrowUpRight size={14} />
          </button>
          <div className="profile">
            <span className="avatar">{actor ? initials(actor.name) : "…"}</span>
            <div>
              <strong>{actor?.name || "Loading…"}</strong>
              {config.auth_mode === "demo" ? (
                <select
                  aria-label="Demo role"
                  value={role}
                  onChange={(e) => {
                    setRole(e.target.value);
                    changeRole(e.target.value);
                  }}
                >
                  <option value="analyst">Analyst · demo</option>
                  <option value="approver">Approver · demo</option>
                  <option value="reviewer">Reviewer · demo</option>
                  <option value="submitter">Submitter · demo</option>
                </select>
              ) : (
                <span>{actor?.role}</span>
              )}
            </div>
            {config.auth_mode === "firebase" && (
              <button
                aria-label="Sign out"
                className="icon-button"
                onClick={signOutUser}
              >
                <LogOut size={17} />
              </button>
            )}
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumbs">
            <button
              className="mobile-toggle icon-button"
              aria-label="Toggle navigation"
              onClick={() => setMobileNav(!mobileNav)}
            >
              <Menu size={20} />
            </button>
            <span>Workspace</span>
            <ChevronRight size={13} />
            <button onClick={() => navigate(page)}>{page}</button>
            {selected && (
              <>
                <ChevronRight size={13} />
                <strong>{detail?.reference || "Case"}</strong>
              </>
            )}
          </div>
          <div className="topbar-right">
            <span className="environment">
              <i />
              {config.model_provider === "demo"
                ? "Local demo"
                : "Vertex AI mode"}
            </span>
            <span className="top-divider" />
            <button
              className="icon-button notifications"
              aria-label={`Open ${approvals.length} pending approvals`}
              onClick={() => navigate("Approvals")}
            >
              <Bell size={18} />
              {approvals.length > 0 && <i />}
            </button>
            <span className="avatar small">
              {actor ? initials(actor.name) : "…"}
            </span>
          </div>
        </header>
        <main>
          {error && (
            <div className="error-box" role="alert">
              <span>{error}</span>
              <button
                onClick={() => {
                  setError("");
                  void refresh();
                }}
              >
                <X size={16} />
              </button>
            </div>
          )}
          {selected ? (
            detail ? (
              <CaseDetail
                detail={detail}
                actor={actor}
                busy={busy}
                perform={perform}
                refresh={refresh}
                back={() => setSelected(null)}
                notify={setToast}
              />
            ) : (
              <div className="loading">
                <LoaderCircle className="spin" />
                Loading case…
              </div>
            )
          ) : (
            <>
              <div className="page-heading">
                <div>
                  <p className="eyebrow">
                    {page === "Overview"
                      ? "A CLEARER PATH TO RESOLUTION"
                      : "REVIEW WORKSPACE"}
                  </p>
                  <h1>
                    {page === "Overview"
                      ? "Every case. A clear next step."
                      : page === "Approvals"
                        ? "Your judgment makes it final."
                        : page === "Documents"
                          ? "Evidence, all in one place."
                          : page === "Activity"
                            ? "An accountable trail."
                            : "Keep every case moving."}
                  </h1>
                  <p>
                    {page === "Overview"
                      ? "Review the evidence. Connect the dots. Resolve with confidence."
                      : page === "Approvals"
                        ? "Independently reviewed recommendations, ready for a human decision."
                        : page === "Documents"
                          ? "Source documents and published policies that ground each review."
                          : page === "Activity"
                            ? "Follow each document, agent review, and human decision."
                            : "From the first document to the final decision, nothing gets lost."}
                  </p>
                </div>
                {canCreate && page !== "Activity" && page !== "Documents" && (
                  <Button kind="primary" onClick={() => setNewCase(true)}>
                    <Plus size={17} />
                    New case
                  </Button>
                )}
              </div>
              {(config.model_provider === "demo" ||
                config.synthetic_workspace) && (
                <div className="demo-banner">
                  <Sparkles size={15} />
                  <span>
                    {config.model_provider === "demo" ? (
                      <>
                        <strong>Local development workspace.</strong> Uses
                        deterministic agents. Switch local roles to try the
                        approval flow.
                      </>
                    ) : (
                      <>
                        <strong>Evaluation workspace.</strong> Seeded records
                        are fictional. Use test documents only; human approval
                        is required for every proposed action.
                      </>
                    )}
                  </span>
                  <button onClick={() => setHelp(true)}>
                    How it works <ArrowRight size={13} />
                  </button>
                </div>
              )}
              {page === "Overview" && (
                <>
                  <div className="stats-grid">
                    <Stat
                      title="Active cases"
                      value={active}
                      caption="Cases moving toward a decision"
                      icon={<FolderOpen size={18} />}
                    />
                    <Stat
                      title="Awaiting approval"
                      value={approvals.length}
                      caption="Ready for your team's judgment"
                      icon={<ShieldCheck size={18} />}
                    />
                    <Stat
                      title="Resolved cases"
                      value={resolved}
                      caption="Reviewed, approved, recorded"
                      icon={<CircleCheck size={18} />}
                    />
                    <Stat
                      title="Documents indexed"
                      value={documents.length}
                      caption={`${documents.filter((d) => d.kind === "policy").length} published review ${documents.filter((d) => d.kind === "policy").length === 1 ? "policy" : "policies"}`}
                      icon={<Files size={18} />}
                    />
                  </div>
                  <div className="section-line">
                    <div>
                      <h2>Your casework</h2>
                      <p>A little context. A lot of clarity.</p>
                    </div>
                    <button
                      className="text-button"
                      onClick={() => navigate("All cases")}
                    >
                      View all cases <ArrowUpRight size={15} />
                    </button>
                  </div>
                </>
              )}
              {["Overview", "All cases", "Approvals"].includes(page) && (
                <div className={page === "Overview" ? "overview-grid" : ""}>
                  <section className="panel cases-panel">
                    <div className="table-toolbar">
                      <div className="filter-tabs">
                        {(page === "Approvals"
                          ? [["all", "Pending decisions"]]
                          : [
                              ["all", "All cases"],
                              ["active", "Active"],
                              ["resolved", "Resolved"],
                            ]
                        ).map(([key, label]) => (
                          <button
                            key={key}
                            className={filter === key ? "selected" : ""}
                            onClick={() => setFilter(key)}
                          >
                            {label}
                            {key === "all" && (
                              <span>
                                {page === "Approvals"
                                  ? approvals.length
                                  : cases.length}
                              </span>
                            )}
                          </button>
                        ))}
                      </div>
                      <div className="table-controls">
                        <label className="search-input">
                          <Search size={15} />
                          <input
                            aria-label="Search cases"
                            placeholder="Search cases…"
                            value={query}
                            onChange={(e) => setQuery(e.target.value)}
                          />
                        </label>
                        <label className="filter-select">
                          <SlidersHorizontal size={15} />
                          <select
                            aria-label="Filter by priority"
                            value={priority}
                            onChange={(e) => setPriority(e.target.value)}
                          >
                            <option value="all">Priority</option>
                            <option value="high">High</option>
                            <option value="medium">Medium</option>
                            <option value="low">Low</option>
                          </select>
                        </label>
                      </div>
                    </div>
                    {loading ? (
                      <div className="loading">
                        <LoaderCircle className="spin" />
                        Loading cases…
                      </div>
                    ) : (
                      <CaseTable cases={filtered} open={openCase} />
                    )}
                    <div className="table-footer">
                      <span>
                        {filtered.length}{" "}
                        {filtered.length === 1 ? "case" : "cases"} in this view
                      </span>
                      <span>
                        <LockKeyhole size={12} /> Access scoped to your
                        workspace
                      </span>
                    </div>
                  </section>
                  {page === "Overview" && (
                    <aside className="insights">
                      <div className="panel review-card">
                        <div className="card-heading">
                          <span className="tiny-icon">
                            <Workflow size={16} />
                          </span>
                          <h3>From evidence to outcome</h3>
                        </div>
                        <div className="review-visual">
                          <div className="orbit orbit-one" />
                          <div className="orbit orbit-two" />
                          <span className="floating-doc">
                            <FileText size={20} />
                          </span>
                          <span className="floating-check">
                            <Check size={17} />
                          </span>
                          <div className="review-center">
                            <FileCheck2 size={30} />
                          </div>
                          <span className="visual-spark">✧</span>
                        </div>
                        <h3>
                          Agents do the groundwork.
                          <br />
                          You make the decision.
                        </h3>
                        <p>
                          Every recommendation connects back to its source.
                          Every action waits for approval.
                        </p>
                        <div className="three-checks">
                          <span>
                            <CheckCheck size={14} />
                            Cited evidence
                          </span>
                          <span>
                            <CheckCheck size={14} />
                            Independent review
                          </span>
                          <span>
                            <CheckCheck size={14} />
                            Human approval
                          </span>
                        </div>
                        <button
                          className="text-button"
                          onClick={() => setHelp(true)}
                        >
                          Explore the review process <ArrowRight size={14} />
                        </button>
                      </div>
                    </aside>
                  )}
                </div>
              )}
              {page === "Overview" && (
                <div className="bottom-grid">
                  <section className="panel activity-preview">
                    <div className="card-heading between">
                      <h3>Latest activity</h3>
                      <button
                        className="text-button"
                        onClick={() => navigate("Activity")}
                      >
                        View activity <ArrowUpRight size={14} />
                      </button>
                    </div>
                    <EventList
                      events={events.slice(0, 4)}
                      open={openCase}
                      compact
                    />
                  </section>
                  <section className="panel pipeline-card">
                    <div className="card-heading between">
                      <h3>A thoughtful review, every time</h3>
                      <span className="muted-label">THE WORKFLOW</span>
                    </div>
                    <div className="pipeline">
                      {[
                        [
                          FileSearch,
                          "Gather evidence",
                          "Find the relevant facts",
                        ],
                        [
                          Sparkles,
                          "Analyze & review",
                          "Build a cited recommendation",
                        ],
                        [
                          ShieldCheck,
                          "Human approval",
                          "Keep people in control",
                        ],
                      ].map(([Icon, label, text], i) => {
                        const I = Icon as typeof FileSearch;
                        return (
                          <div className="pipeline-step" key={i}>
                            <div className={`pipeline-icon step-${i}`}>
                              <I size={19} />
                            </div>
                            <strong>{String(label)}</strong>
                            <span>{String(text)}</span>
                            {i < 2 && (
                              <ChevronRight
                                className="pipeline-arrow"
                                size={17}
                              />
                            )}
                          </div>
                        );
                      })}
                    </div>
                    <div className="pipeline-note">
                      <span className="live-dot" />
                      {config.model_provider === "demo"
                        ? "Persistent local workflow"
                        : "GCP workflow"}
                      <span>Evidence → analysis → review → approval</span>
                    </div>
                  </section>
                </div>
              )}
              {page === "Documents" && (
                <DocumentsPage
                  documents={documents}
                  cases={cases}
                  actor={actor}
                  refresh={refresh}
                  error={setError}
                  notify={setToast}
                />
              )}
              {page === "Activity" && (
                <section className="panel full-activity">
                  <div className="card-heading between">
                    <h3>Workspace activity</h3>
                    <span className="muted-label">
                      LATEST {events.length} EVENTS
                    </span>
                  </div>
                  <EventList events={events} open={openCase} />
                </section>
              )}
            </>
          )}
          <footer className="page-footer">
            <span>
              <span className="footer-dot" />
              Doci · Clarity in every case
            </span>
            <span>Evidence-led. Human-approved.</span>
          </footer>
        </main>
      </div>
      {newCase && (
        <CreateCaseModal
          close={() => setNewCase(false)}
          created={async (id) => {
            setNewCase(false);
            openCase(id);
            await refresh();
            setToast("Case created. Add documents to begin your review.");
          }}
        />
      )}
      {help && (
        <Modal
          title="A clear path through every case"
          subtitle="One connected workflow, with your team in control."
          close={() => setHelp(false)}
        >
          <div className="guide-steps">
            {[
              [
                "01",
                "Bring the evidence",
                "Create a case and upload PDF or text documents. Reviewers can publish policies in the Documents library.",
              ],
              [
                "02",
                "Start an independent review",
                "The workflow retrieves passages, drafts a cited proposal, and checks it through a separate reviewer. Missing evidence is escalated.",
              ],
              [
                "03",
                "Make the human decision",
                "An approver reviews the exact proposal and gives a reason to approve or reject it. Submitters and requesting analysts cannot approve their own work.",
              ],
              [
                "04",
                "Keep the record",
                "The approved internal disposition is recorded once, with an audit trail. No payments, account changes, or external messages are performed.",
              ],
            ].map(([number, title, body]) => (
              <div key={number}>
                <b>{number}</b>
                <section>
                  <h3>{title}</h3>
                  <p>{body}</p>
                </section>
              </div>
            ))}
          </div>
          {config.model_provider === "demo" && (
            <div className="note-box">
              <Sparkles size={18} />
              <p>
                In this demo, choose <strong>Approver · demo</strong> beneath
                your name in the sidebar to try a pending decision. Demo
                confidence values are illustrative.
              </p>
            </div>
          )}
          <div className="modal-actions">
            <Button kind="primary" onClick={() => setHelp(false)}>
              Got it <Check size={16} />
            </Button>
          </div>
        </Modal>
      )}
      {toast && (
        <div className="toast" role="status">
          <CircleCheck size={18} />
          {toast}
          <button
            aria-label="Dismiss notification"
            onClick={() => setToast("")}
          >
            <X size={15} />
          </button>
        </div>
      )}
    </div>
  );
}

function Stat({
  title,
  value,
  caption,
  icon,
}: {
  title: string;
  value: number;
  caption: string;
  icon: ReactNode;
}) {
  return (
    <div className="stat-card">
      <div>
        <span>{title}</span>
        <div className="stat-icon">{icon}</div>
      </div>
      <strong>{String(value).padStart(2, "0")}</strong>
      <p>{caption}</p>
    </div>
  );
}

function CaseTable({
  cases,
  open,
}: {
  cases: Case[];
  open: (id: string) => void;
}) {
  if (!cases.length)
    return (
      <Empty
        title="A clear view"
        text="No cases match this view. Try another filter or create a new case."
      />
    );
  return (
    <div className="table-scroll">
      <table className="case-table">
        <thead>
          <tr>
            <th>CASE DETAILS</th>
            <th>STATUS</th>
            <th>PRIORITY</th>
            <th>ADDED</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {cases.map((c) => (
            <tr key={c.id} onClick={() => open(c.id)}>
              <td>
                <div className="case-cell">
                  <span
                    className={`case-icon ${c.category === "Transaction dispute" ? "dispute" : c.category === "Policy exception" ? "policy" : ""}`}
                  >
                    <FileText size={18} />
                  </span>
                  <div>
                    <button
                      className="case-title"
                      onClick={(e) => {
                        e.stopPropagation();
                        open(c.id);
                      }}
                    >
                      {c.title}
                    </button>
                    <span className="case-meta">
                      <span>{c.reference}</span>
                      <i />
                      {c.document_count}{" "}
                      {c.document_count === 1 ? "document" : "documents"}
                    </span>
                  </div>
                </div>
              </td>
              <td>
                <Badge status={c.status} />
              </td>
              <td>
                <span className={`priority ${c.priority}`}>
                  <i />
                  <i />
                  <i />
                  {c.priority}
                </span>
              </td>
              <td className="date-cell">{date(c.created_at)}</td>
              <td>
                <ChevronRight size={15} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EventList({
  events,
  open,
  compact = false,
}: {
  events: Event[];
  open: (id: string) => void;
  compact?: boolean;
}) {
  if (!events.length)
    return (
      <Empty
        title="Your audit trail starts here"
        text="Case and document activity will appear as your team works."
        icon={<Activity size={25} />}
      />
    );
  return (
    <div className={`event-list ${compact ? "compact" : ""}`}>
      {events.map((event) => (
        <div className="event" key={event.id}>
          <span
            className={`event-icon ${event.event.includes("approval") || event.event === "action.recorded" ? "completed" : ""}`}
          >
            {event.event.includes("approval") ||
            event.event === "action.recorded" ? (
              <ShieldCheck size={14} />
            ) : event.event.includes("document") ? (
              <FileText size={14} />
            ) : (
              <Workflow size={14} />
            )}
          </span>
          <div>
            <button
              onClick={() => event.case_id && open(event.case_id)}
              disabled={!event.case_id}
            >
              {event.detail}
            </button>
            <span>
              {event.event.replaceAll(".", " · ").replaceAll("_", " ")}
              <i />
              {event.actor.replace("demo-", "").replace("agent:", "")}
            </span>
          </div>
          <time title={date(event.created_at)}>{time(event.created_at)}</time>
        </div>
      ))}
    </div>
  );
}

function CreateCaseModal({
  close,
  created,
}: {
  close: () => void;
  created: (id: string) => Promise<void>;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      const result = await api<Case>("/cases", {
        method: "POST",
        body: JSON.stringify(Object.fromEntries(form)),
      });
      await created(result.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title="Make room for a new case"
      subtitle="Give your review a name and a little context."
      close={close}
    >
      <form onSubmit={submit}>
        <label>
          Case title
          <input
            autoFocus
            required
            minLength={3}
            maxLength={200}
            name="title"
            placeholder="e.g. Quarterly document review"
          />
        </label>
        <div className="form-row">
          <label>
            Category
            <select name="category">
              <option>Document review</option>
              <option>Customer onboarding</option>
              <option>Transaction dispute</option>
              <option>Policy exception</option>
            </select>
          </label>
          <label>
            Priority
            <select name="priority" defaultValue="medium">
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </label>
        </div>
        <label>
          What needs to be reviewed?
          <textarea
            required
            minLength={10}
            maxLength={10000}
            name="description"
            rows={4}
            placeholder="Describe the case, what needs to be checked, and any questions to resolve."
          />
        </label>
        <p className="form-hint">
          <LockKeyhole size={13} />
          You can add supporting documents after creating the case.
        </p>
        {error && (
          <div role="alert" className="error-box">
            {error}
          </div>
        )}
        <div className="modal-actions">
          <Button onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button kind="primary" type="submit" disabled={busy}>
            {busy ? (
              <LoaderCircle size={16} className="spin" />
            ) : (
              <Plus size={16} />
            )}
            Create case
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function DocumentsPage({
  documents,
  cases,
  actor,
  refresh,
  error,
  notify,
}: {
  documents: Document[];
  cases: Case[];
  actor: Actor | null;
  refresh: () => Promise<void>;
  error: (s: string) => void;
  notify: (s: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("all");
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const visible = documents.filter(
    (d) =>
      (kind === "all" || d.kind === kind) &&
      d.filename.toLowerCase().includes(search.toLowerCase()),
  );
  async function upload(file: File) {
    setBusy(true);
    const data = new FormData();
    data.append("file", file);
    try {
      await api("/documents", { method: "POST", body: data });
      await refresh();
      notify("Policy published and indexed.");
    } catch (e) {
      error((e as Error).message);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }
  return (
    <section className="panel">
      <div className="table-toolbar">
        <div className="filter-tabs">
          {[
            ["all", "All documents"],
            ["evidence", "Case evidence"],
            ["policy", "Policies"],
          ].map(([value, label]) => (
            <button
              key={value}
              onClick={() => setKind(value)}
              className={kind === value ? "selected" : ""}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="table-controls">
          <label className="search-input">
            <Search size={15} />
            <input
              aria-label="Search documents"
              placeholder="Find a document…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          {actor && ["reviewer", "admin"].includes(actor.role) && (
            <>
              <input
                ref={fileRef}
                className="visually-hidden"
                type="file"
                accept=".pdf,.txt,.md,.csv"
                onChange={(e) => {
                  if (e.target.files?.[0]) void upload(e.target.files[0]);
                }}
              />
              <Button onClick={() => fileRef.current?.click()} disabled={busy}>
                <Upload size={15} />
                {busy ? "Indexing…" : "Publish policy"}
              </Button>
            </>
          )}
        </div>
      </div>
      {!visible.length ? (
        <Empty
          title="No documents here yet"
          text="Upload evidence inside a case, or publish a policy as a reviewer."
          icon={<Files size={28} />}
        />
      ) : (
        <div className="table-scroll">
          <table className="document-table">
            <thead>
              <tr>
                <th>DOCUMENT</th>
                <th>COLLECTION</th>
                <th>SIZE</th>
                <th>ADDED</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {visible.map((doc) => (
                <tr key={doc.id}>
                  <td>
                    <div className="case-cell">
                      <span
                        className={`case-icon ${doc.kind === "policy" ? "policy" : ""}`}
                      >
                        {doc.kind === "policy" ? (
                          <BookOpen size={18} />
                        ) : (
                          <FileText size={18} />
                        )}
                      </span>
                      <div>
                        <strong>{doc.filename}</strong>
                        <span className="case-meta">
                          {doc.pages} {doc.pages === 1 ? "page" : "pages"}
                          <i />
                          <span className="indexed">
                            <Check size={11} />
                            Indexed
                          </span>
                        </span>
                      </div>
                    </div>
                  </td>
                  <td>
                    {doc.kind === "policy" ? (
                      <span className="policy-label">Published policy</span>
                    ) : (
                      cases.find((c) => c.id === doc.case_id)?.reference ||
                      "Case evidence"
                    )}
                  </td>
                  <td>{formatBytes(doc.size)}</td>
                  <td>{date(doc.created_at)}</td>
                  <td>
                    <button
                      className="icon-button"
                      aria-label={`Download ${doc.filename}`}
                      onClick={() =>
                        void downloadDocument(doc.id, doc.filename).catch((e) =>
                          error(e.message),
                        )
                      }
                    >
                      <ArrowDownToLine size={17} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="table-footer">
        <span>{visible.length} documents</span>
        <span>PDF, TXT, Markdown and CSV · 15 MB maximum</span>
      </div>
    </section>
  );
}

function CaseDetail({
  detail,
  actor,
  busy,
  perform,
  refresh,
  back,
  notify,
}: {
  detail: Detail;
  actor: Actor | null;
  busy: boolean;
  perform: (fn: () => Promise<unknown>, message: string) => Promise<void>;
  refresh: () => Promise<void>;
  back: () => void;
  notify: (s: string) => void;
}) {
  const [tab, setTab] = useState("Overview");
  const [decision, setDecision] = useState<"approve" | "reject" | null>(null);
  const [focus, setFocus] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const run = detail.run;
  const canStart =
    actor && ["analyst", "reviewer", "admin"].includes(actor.role);
  const canUpload =
    actor &&
    ["analyst", "submitter", "admin"].includes(actor.role) &&
    editable.includes(detail.status) &&
    detail.status !== "failed";
  const canDecide =
    actor &&
    ["approver", "admin"].includes(actor.role) &&
    actor.id !== detail.created_by &&
    actor.id !== run?.requested_by &&
    detail.status === "awaiting_approval";
  async function upload(file: File) {
    const data = new FormData();
    data.append("file", file);
    data.append("case_id", detail.id);
    await perform(
      () => api("/documents", { method: "POST", body: data }),
      "Document uploaded and indexed.",
    );
    if (fileRef.current) fileRef.current.value = "";
  }
  const steps = [
    "evidence",
    "analysis",
    "independent_review",
    "human_approval",
    "complete",
  ];
  const stepIndex = run ? steps.indexOf(run.step) : -1;
  return (
    <>
      <button className="back-link" onClick={back}>
        <ArrowLeft size={15} />
        Back to workspace
      </button>
      <div className="detail-heading">
        <div>
          <div className="detail-meta">
            <span className="mono">{detail.reference}</span>
            <span> / </span>
            <span>{detail.category}</span>
            <Badge status={detail.status} />
          </div>
          <h1>{detail.title}</h1>
          <p>{detail.description}</p>
        </div>
        <div className="detail-actions">
          {canUpload && (
            <>
              <input
                ref={fileRef}
                type="file"
                className="visually-hidden"
                accept=".pdf,.txt,.md,.csv"
                onChange={(e) => {
                  if (e.target.files?.[0]) void upload(e.target.files[0]);
                }}
              />
              <Button onClick={() => fileRef.current?.click()} disabled={busy}>
                <Upload size={16} />
                Add document
              </Button>
            </>
          )}
          {canStart &&
          (detail.status === "failed" ||
            detail.status === "queued" ||
            detail.status === "processing") &&
          run ? (
            <Button
              kind="primary"
              disabled={busy}
              onClick={() =>
                void perform(
                  () => api(`/runs/${run.id}/retry`, { method: "POST" }),
                  "Saved review dispatched for retry.",
                )
              }
            >
              <Workflow size={16} />
              Retry review
            </Button>
          ) : (
            canStart &&
            editable.includes(detail.status) && (
              <Button
                kind="primary"
                disabled={busy}
                onClick={() =>
                  void perform(
                    () =>
                      api(`/cases/${detail.id}/reviews`, { method: "POST" }),
                    "Review started. Your workspace updates automatically.",
                  )
                }
              >
                <Sparkles size={16} />
                Start review
              </Button>
            )
          )}
        </div>
      </div>
      <section className="panel workflow-strip">
        {steps.map((step, i) => {
          const skipped =
            i === 3 && detail.status === "escalated" && !detail.decision;
          const done =
            !skipped &&
            (stepIndex > i ||
              (i === 4 &&
                [
                  "resolved",
                  "rejected",
                  "escalated",
                  "needs_information",
                ].includes(detail.status)));
          return (
            <div
              className={`workflow-stage ${done ? "done" : !skipped && stepIndex === i ? "current" : ""}`}
              key={step}
            >
              <span>{done ? <Check size={14} /> : i + 1}</span>
              <div>
                <strong>
                  {
                    [
                      "Evidence",
                      "Analysis",
                      "Independent review",
                      "Human decision",
                      "Recorded outcome",
                    ][i]
                  }
                </strong>
                <small>
                  {skipped
                    ? "Not reached"
                    : done
                      ? "Complete"
                      : stepIndex === i
                        ? statusLabel[run?.status || "draft"]
                        : "Pending"}
                </small>
              </div>
              {i < 4 && <ChevronRight size={15} />}
            </div>
          );
        })}
      </section>
      {run?.mode === "demo" && (
        <div className="demo-banner">
          <Sparkles size={15} />
          <span>
            This review uses deterministic demo agents. Findings and confidence
            are illustrative, and require human assessment.
          </span>
        </div>
      )}
      {run?.error && <div className="error-box">{run.error}</div>}
      <div className="detail-grid">
        <section>
          <div className="detail-tabs">
            {["Overview", "Evidence", "Activity"].map((name) => (
              <button
                key={name}
                onClick={() => setTab(name)}
                className={tab === name ? "selected" : ""}
              >
                {name}
                {name === "Evidence" && (
                  <span>{run?.evidence.length || 0}</span>
                )}
              </button>
            ))}
          </div>
          {tab === "Overview" && (
            <>
              {run?.analysis.summary ? (
                <section className="panel analysis-panel">
                  <div className="card-heading between">
                    <div className="heading-with-icon">
                      <span className="tiny-icon">
                        <Sparkles size={17} />
                      </span>
                      <h3>Agent analysis</h3>
                    </div>
                    <span className="muted-label">
                      {run.mode === "demo" ? "DEMO ANALYST" : "GEMINI ANALYST"}
                    </span>
                  </div>
                  <p className="analysis-summary">{run.analysis.summary}</p>
                  <h4>What the evidence tells us</h4>
                  <div className="findings">
                    {run.analysis.findings?.map((finding, i) => (
                      <div className="finding" key={i}>
                        <span>{String(i + 1).padStart(2, "0")}</span>
                        <div>
                          <p>{finding.statement}</p>
                          <div className="citation-links">
                            {finding.citation_ids.map((id) => {
                              const evidence = run.evidence.find(
                                (e) => e.chunk_id === id,
                              );
                              return (
                                <button
                                  key={id}
                                  onClick={() => {
                                    setTab("Evidence");
                                    setFocus(id);
                                  }}
                                >
                                  <FileText size={12} />
                                  {evidence
                                    ? `${evidence.filename} · p. ${evidence.page}`
                                    : "Source unavailable"}
                                  <ArrowUpRight size={11} />
                                </button>
                              );
                            })}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                  {!!run.analysis.missing_information?.length && (
                    <div className="note-box warning">
                      <HelpCircle size={18} />
                      <div>
                        <strong>Information still needed</strong>
                        {run.analysis.missing_information.map((text, i) => (
                          <p key={i}>{text}</p>
                        ))}
                      </div>
                    </div>
                  )}
                </section>
              ) : (
                <section className="panel">
                  <Empty
                    title={
                      run
                        ? "The review is underway"
                        : "A good review starts with evidence"
                    }
                    text={
                      run
                        ? "The agents are gathering evidence and building a recommendation. This page updates automatically."
                        : "Add supporting documents, then start the review to build a cited recommendation."
                    }
                    icon={
                      run ? (
                        <LoaderCircle className="spin" size={28} />
                      ) : (
                        <FileSearch size={28} />
                      )
                    }
                  />
                </section>
              )}
              {run?.review.verdict && (
                <section className="panel reviewer-panel">
                  <div className="card-heading between">
                    <div className="heading-with-icon">
                      <ShieldCheck size={19} />
                      <h3>Independent review</h3>
                    </div>
                    <span className={`review-verdict ${run.review.verdict}`}>
                      {run.review.verdict === "pass" ? (
                        <CheckCheck size={14} />
                      ) : (
                        <HelpCircle size={14} />
                      )}
                      {run.review.verdict}
                    </span>
                  </div>
                  <p>{run.review.reasoning}</p>
                  {run.review.concerns?.map((c, i) => (
                    <div className="review-concern" key={i}>
                      {c}
                    </div>
                  ))}
                </section>
              )}
            </>
          )}
          {tab === "Evidence" && (
            <EvidencePanel
              evidence={run?.evidence || []}
              focus={focus}
              caseId={detail.id}
            />
          )}
          {tab === "Activity" && (
            <section className="panel full-activity">
              <EventList events={detail.events} open={() => {}} />
            </section>
          )}
          <section className="panel source-panel">
            <div className="card-heading between">
              <h3>Source documents</h3>
              <span className="muted-label">
                {detail.documents.length} FILES
              </span>
            </div>
            {detail.documents.length ? (
              detail.documents.map((doc) => (
                <div className="source-document" key={doc.id}>
                  <span className="case-icon">
                    <FileText size={17} />
                  </span>
                  <div>
                    <strong>{doc.filename}</strong>
                    <span>
                      {formatBytes(doc.size)} · {doc.pages}{" "}
                      {doc.pages === 1 ? "page" : "pages"} · Indexed
                    </span>
                  </div>
                  <button
                    className="icon-button"
                    aria-label={`Download ${doc.filename}`}
                    onClick={() =>
                      void perform(
                        () => downloadDocument(doc.id, doc.filename),
                        "Document downloaded.",
                      )
                    }
                  >
                    <ArrowDownToLine size={17} />
                  </button>
                </div>
              ))
            ) : (
              <p className="empty-inline">
                No supporting documents have been added.
              </p>
            )}
          </section>
        </section>
        <aside>
          <section className="panel decision-panel">
            <div className="card-heading">
              <span className="tiny-icon emphasis">
                <FileCheck2 size={17} />
              </span>
              <h3>Proposed next step</h3>
            </div>
            {run?.analysis.action_title ? (
              <>
                <h2>{run.analysis.action_title}</h2>
                <p>{run.analysis.rationale}</p>
                <div className="confidence">
                  <div>
                    <span>Agent confidence</span>
                    <strong>
                      {Math.round((run.confidence || 0) * 100)}
                      <small>%</small>
                    </strong>
                  </div>
                  <div className="confidence-bar">
                    <i style={{ width: `${(run.confidence || 0) * 100}%` }} />
                  </div>
                  <span>
                    {run.mode === "demo"
                      ? "Illustrative demo value"
                      : "Model estimate · not a calibrated probability"}
                  </span>
                </div>
                {detail.decision ? (
                  <div
                    className={`decision-record ${detail.decision.decision}`}
                  >
                    <ShieldCheck size={18} />
                    <div>
                      <strong>
                        {detail.decision.decision === "approve"
                          ? "Human approval recorded"
                          : "Recommendation rejected"}
                      </strong>
                      <p>{detail.decision.comment}</p>
                    </div>
                  </div>
                ) : detail.status === "awaiting_approval" ? (
                  <>
                    <div className="approval-hint">
                      <LockKeyhole size={14} />
                      <span>Awaiting an independent human decision</span>
                    </div>
                    {canDecide ? (
                      <div className="decision-buttons">
                        <Button
                          kind="primary"
                          disabled={busy}
                          onClick={() => setDecision("approve")}
                        >
                          <Check size={16} />
                          Approve proposal
                        </Button>
                        <Button
                          disabled={busy}
                          onClick={() => setDecision("reject")}
                        >
                          <X size={16} />
                          Reject
                        </Button>
                      </div>
                    ) : (
                      <p className="form-hint">
                        An independent approver can approve or reject this
                        proposal.{" "}
                        {actor?.role !== "approver" &&
                          run.mode === "demo" &&
                          "Switch to the approver demo role in the sidebar."}
                      </p>
                    )}
                  </>
                ) : (
                  <div className="approval-hint">
                    <ShieldCheck size={16} />
                    <span>
                      {detail.status === "escalated"
                        ? "Human investigation required"
                        : "A decision will be available after review"}
                    </span>
                  </div>
                )}
                <div className="action-scope">
                  This workflow records an internal disposition. External
                  transactions and messages are outside its scope.
                </div>
              </>
            ) : (
              <p>
                Your proposed next step will appear after the agents finish
                their review.
              </p>
            )}
          </section>
          <section className="panel case-info">
            <h3>Case details</h3>
            <dl>
              <dt>Priority</dt>
              <dd>
                <span className={`priority ${detail.priority}`}>
                  <i />
                  <i />
                  <i />
                  {detail.priority}
                </span>
              </dd>
              <dt>Created</dt>
              <dd>{date(detail.created_at)}</dd>
              <dt>Assigned to</dt>
              <dd>{detail.assigned_to}</dd>
              <dt>Review version</dt>
              <dd>{run ? `${run.revision + 1}` : "Not started"}</dd>
            </dl>
            {run && (
              <div className="trace-reference">
                <span>Trace reference</span>
                <code title={run.trace_id}>{run.trace_id.slice(0, 18)}…</code>
              </div>
            )}
          </section>
        </aside>
      </div>
      {decision && run && (
        <DecisionModal
          decision={decision}
          detail={detail}
          close={() => setDecision(null)}
          complete={async () => {
            setDecision(null);
            await refresh();
            notify("Decision recorded. The workflow is resuming.");
          }}
        />
      )}
    </>
  );
}

function DecisionModal({
  decision,
  detail,
  close,
  complete,
}: {
  decision: "approve" | "reject";
  detail: Detail;
  close: () => void;
  complete: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const data = new FormData(event.currentTarget);
    try {
      await api(`/runs/${detail.run!.id}/decision`, {
        method: "POST",
        body: JSON.stringify({
          decision,
          comment: data.get("comment"),
          proposal_hash: detail.run!.proposal_hash,
        }),
      });
      await complete();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={
        decision === "approve"
          ? "Approve this proposal"
          : "Return this recommendation"
      }
      subtitle={detail.title}
      close={close}
    >
      <div className="proposal-preview">
        <span>REVIEWED PROPOSAL</span>
        <strong>{detail.run?.analysis.action_title}</strong>
        <p>{detail.run?.analysis.summary}</p>
      </div>
      <form onSubmit={submit}>
        <label>
          Your decision rationale
          <textarea
            autoFocus
            required
            minLength={3}
            maxLength={2000}
            name="comment"
            rows={4}
            placeholder={
              decision === "approve"
                ? "Explain why the evidence supports this disposition…"
                : "Explain what needs to change or be investigated…"
            }
          />
        </label>
        <div className="note-box">
          <ShieldCheck size={18} />
          <p>
            Your identity and reason will be recorded with this exact proposal
            in the audit trail.
          </p>
        </div>
        {error && (
          <div role="alert" className="error-box">
            {error}
          </div>
        )}
        <div className="modal-actions">
          <Button onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button
            type="submit"
            kind={decision === "approve" ? "primary" : "danger"}
            disabled={busy}
          >
            {busy ? (
              <LoaderCircle size={16} className="spin" />
            ) : (
              <Check size={16} />
            )}
            {decision === "approve" ? "Confirm approval" : "Confirm rejection"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function EvidencePanel({
  evidence,
  focus,
  caseId,
}: {
  evidence: Evidence[];
  focus: string;
  caseId: string;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Evidence[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function search(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      setResults(
        await api<Evidence[]>(
          `/cases/${caseId}/search?q=${encodeURIComponent(query)}`,
        ),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const visible = results || evidence;
  return (
    <section className="panel evidence-panel">
      <div className="card-heading">
        <FileSearch size={19} />
        <h3>{results ? "Search results" : "Cited evidence"}</h3>
      </div>
      <form className="evidence-search" onSubmit={search}>
        <label className="search-input">
          <Search size={16} />
          <input
            minLength={2}
            required
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search this case and published policies…"
            aria-label="Search evidence"
          />
        </label>
        <Button type="submit" disabled={busy}>
          {busy ? <LoaderCircle className="spin" size={15} /> : "Search"}
        </Button>
        {results && (
          <button
            type="button"
            className="icon-button"
            aria-label="Clear evidence search"
            onClick={() => {
              setResults(null);
              setQuery("");
            }}
          >
            <X size={16} />
          </button>
        )}
      </form>
      {error && (
        <div role="alert" className="error-box">
          {error}
        </div>
      )}
      {!visible.length ? (
        <Empty
          title="No passages yet"
          text="Start a review or search uploaded documents to find relevant evidence."
          icon={<FileSearch size={25} />}
        />
      ) : (
        visible.map((item, i) => (
          <article
            className={`evidence-item ${focus === item.chunk_id ? "focused" : ""}`}
            key={item.chunk_id}
          >
            <div>
              <span className="evidence-number">
                {String(i + 1).padStart(2, "0")}
              </span>
              <strong>{item.filename}</strong>
              <span className={`source-tag ${item.kind}`}>
                {item.kind === "policy" ? "Policy" : "Case evidence"}
              </span>
            </div>
            <blockquote>{item.quote}</blockquote>
            <footer>
              <span>Page {item.page}</span>
              <span>Source passage · {item.chunk_id.slice(0, 8)}</span>
            </footer>
          </article>
        ))
      )}
    </section>
  );
}
