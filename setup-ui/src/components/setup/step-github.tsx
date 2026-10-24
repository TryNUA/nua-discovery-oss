"use client";

import { useState } from "react";
import { CircleCheck, RefreshCw } from "lucide-react";

import { api, problemsOf, useApi, type DeployInfo, type GithubStatus, type ReceiverInfo } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { CollapsibleSection } from "@/components/ui/collapsible";
import { Spinner } from "@/components/ui/spinner";
import { Text } from "@/components/ui/text";
import { Field, LinkButton, Note, Section, SetBadge } from "@/components/setup/common";
import { StepFrame, type StepNav } from "@/components/setup/frame";
import { useDraft, useProblems, useSaver, type FieldProblem } from "@/components/setup/store";

const RUNNING = ["starting", "waiting", "installing"];

function Progress({ status }: { status: GithubStatus }) {
  return (
    <div className="flex flex-col gap-3 rounded-md border border-rule bg-bg-raised shadow-xs p-4">
      {status.status === "starting" && (
        <div className="flex items-center gap-2">
          <Spinner size="sm" />
          <Text size="sm">Preparing the app manifest...</Text>
        </div>
      )}
      {status.status === "waiting" && status.url && (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <Spinner size="sm" />
            <Text size="sm">Waiting for you on GitHub: name the app and click Create GitHub App.</Text>
          </div>
          <div>
            <LinkButton href={status.url} variant="primary">
              Open GitHub
            </LinkButton>
          </div>
          <Text size="xs" color="faint">
            Sign in as the repo owner (or an owner of its organization) first. The app is created on that account.
          </Text>
        </div>
      )}
      {status.status === "installing" && (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <CircleCheck className="w-4 h-4 text-success" />
            <Text size="sm">App created. Key and webhook secret are in .env; ids are in tfvars.</Text>
          </div>
          <div className="flex items-center gap-2">
            <Spinner size="sm" />
            <Text size="sm">Now install it on your repo: Only select repositories, pick the repo, Install.</Text>
          </div>
          {status.install_url && (
            <div>
              <LinkButton href={status.install_url} variant="primary">
                Install the app
              </LinkButton>
            </div>
          )}
        </div>
      )}
      {status.status === "done" && (
        <div className="flex items-center gap-2">
          <CircleCheck className="w-4 h-4 text-success" />
          <Text size="sm">Installed. Installation id {status.installation_id} is in tfvars.</Text>
        </div>
      )}
      {status.status === "error" && status.error && <Note title="That did not work" description={status.error} variant="danger" />}
    </div>
  );
}

export function StepGithub({
  deploy,
  nav,
  onSaved,
  onCheck,
}: {
  deploy: DeployInfo;
  nav: StepNav;
  onSaved: () => void;
  onCheck: () => void;
}) {
  const step = "github" as const;
  const [repo, setRepo] = useDraft(step, "repo", () => deploy.repo);
  const [owner, setOwner] = useDraft(step, "owner", () => "");
  const [name, setName] = useDraft(step, "name", () => "");
  const [problems, setProblems] = useState<string[]>([]);
  const { error } = useProblems(step);
  const [busy, setBusy] = useState(false);
  const receiver = useApi<ReceiverInfo>("receiver");
  const { data: status, mutate } = useApi<GithubStatus>("github", 2000);
  const running = Boolean(status && RUNNING.includes(status.status));
  const repoOwner = repo.includes("/") ? repo.split("/")[0].trim() : "";
  const appOwner = owner.trim() || repoOwner;
  const appName = name.trim() || status?.default_name || "";
  const saved = status?.saved;
  const nameProblem = appName.length > 34 ? "At most 34 characters (a GitHub limit)." : undefined;

  // the repo is the only field here that lives in a file (state and tfvars); owner and name are used on Create
  useSaver(step, () =>
    api<DeployInfo & { problems: FieldProblem[] }>("deploy", "PUT", { repo: repo.trim() }).then((r) => r.problems || []),
  );

  function start() {
    setProblems([]);
    setBusy(true);
    api<GithubStatus>("github/start", "POST", { repo, owner: appOwner, name: appName })
      .then((s) => mutate(s))
      .catch((e) => setProblems(problemsOf(e)))
      .finally(() => setBusy(false));
  }

  function checkInstall() {
    setProblems([]);
    setBusy(true);
    api<GithubStatus>("github/check-install", "POST")
      .then((s) => {
        mutate(s);
        onSaved();
      })
      .catch((e) => setProblems(problemsOf(e)))
      .finally(() => setBusy(false));
  }

  return (
    <StepFrame
      nav={nav}
      title="GitHub App"
      intro="Lets the pipeline commit to your repo and sends push webhooks to the receiver. One click on GitHub creates it with the right permissions."
      aside={
        <>
          <Button onClick={start} loading={busy || running} disabled={!receiver.data?.url || running || Boolean(nameProblem)}>
            {saved?.app && "Create a new GitHub App"}
            {!saved?.app && "Create GitHub App"}
          </Button>
          {saved?.app && (
            <Button variant="secondary" onClick={checkInstall} loading={busy} disabled={running}>
              Check installation
            </Button>
          )}
        </>
      }
    >
      {problems.length > 0 && (
        <div className="pt-6">
          <Note title="That did not work" description={problems.join(" ")} variant="danger" />
        </div>
      )}
      <Section title="Where it points">
        {receiver.isLoading && (
          <div className="flex items-center gap-2">
            <Spinner size="sm" />
            <Text size="sm" color="faint">
              Looking up the receiver URL...
            </Text>
          </div>
        )}
        {receiver.data?.url && (
          <div className="flex flex-col gap-1">
            <Text size="sm" color="muted">
              Webhook URL
            </Text>
            <Text size="sm" mono className="break-all">
              {receiver.data.url}/github
            </Text>
          </div>
        )}
        {receiver.data && !receiver.data.url && <Note title="No receiver URL yet" description={receiver.data.how_to} variant="warn" />}
      </Section>
      <Section title="Repo and app">
        <div className="flex flex-col gap-3">
          <Field
            id="gh-repo"
            label="Your private repo"
            mono
            placeholder="acme/discovery"
            value={repo}
            error={error("repo")}
            onChange={setRepo}
            required
          />
          <Text size="sm" color="muted">
            Creates the app{" "}
            <Text size="sm" mono>
              {appName || "..."}
            </Text>{" "}
            owned by{" "}
            <Text size="sm" mono>
              {appOwner || "the repo owner"}
            </Text>
            .
          </Text>
          <CollapsibleSection title="Change">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <Field
                id="gh-owner"
                label="App owner"
                mono
                placeholder={repoOwner || "the repo owner"}
                hint="The account or organization that owns the repo; otherwise the app cannot be installed on it."
                value={owner}
                onChange={setOwner}
              />
              <Field
                id="gh-name"
                label="App name"
                mono
                placeholder={status?.default_name || "nua-discovery"}
                hint="Unique on GitHub, at most 34 characters. Commits show as <name>[bot]."
                value={name}
                error={nameProblem}
                onChange={setName}
              />
            </div>
          </CollapsibleSection>
        </div>
      </Section>
      <Section title="Status">
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap gap-x-6 gap-y-2">
            <div className="flex items-center gap-2">
              <Text size="sm" color="muted">
                Key and webhook secret in .env
              </Text>
              <SetBadge set={Boolean(saved?.app)} />
            </div>
            <div className="flex items-center gap-2">
              <Text size="sm" color="muted">
                Installation id
              </Text>
              <SetBadge set={Boolean(saved?.installation_id)} />
            </div>
            <Button variant="outline" size="sm" type="button" onClick={() => {
                mutate();
                onCheck();
              }}>
              <RefreshCw className="w-3.5 h-3.5" />
              Check again
            </Button>
          </div>
          {status && status.status !== "idle" && <Progress status={status} />}
          {status?.status === "idle" && saved?.app && !saved.installation_id && status.install_url && (
            <div className="flex flex-col gap-2">
              <Text size="sm">The app exists but is not installed yet.</Text>
              <div>
                <LinkButton href={status.install_url}>Install the app</LinkButton>
              </div>
            </div>
          )}
          <Text size="xs" color="faint" leading="relaxed">
            GitHub pings the webhook right away. That delivery fails until the receiver is deployed, which is fine:
            redeliver it after step 6.
          </Text>
        </div>
      </Section>
    </StepFrame>
  );
}
