#!/usr/bin/env python3
"""One-shot script for vault wiki-link migration after restructure."""
import pathlib
import re

ADR_MAP = {
    "001": "ADR-001-notifier-protocol-not-abc",
    "002": "ADR-002-plugin-discovery-explicit-registry",
    "003": "ADR-003-error-strategy-exceptions-result-for-notifier",
    "004": "ADR-004-composition-root-container-infra-services",
    "005": "ADR-005-concurrency-soft-yield-retry-busy",
    "006": "ADR-006-import-linter-ci",
    "007": "ADR-007-per-connection-pragma",
    "008": "ADR-008-eventbus-dual-circuit-no-db-persistence",
    "009": "ADR-009-backup-user-state-tables-only",
    "010": "ADR-010-data-dir-location-policy",
    "011": "ADR-011-dns-rebinding-host-allowlist",
    "012": "ADR-012-diagnostic-zip-allowlist-redactor",
    "013": "ADR-013-locker-os-level-pid-info-only",
    "014": "ADR-014-two-phase-shutdown",
    "015": "ADR-015-smtp-host-validation",
    "016": "ADR-016-repository-invariants-begin-immediate",
    "017": "ADR-017-secrets-secretstr-crash-dump-exclusion",
    "018": "ADR-018-onboarding-fsm-server-enforced",
    "019": "ADR-019-notification-state-machine",
    "020": "ADR-020-smtp-host-port-ssot-state-db",
    "021": "ADR-021-manual-starttls-connect-by-ip",
    "022": "ADR-022-allowed-tracked-fields-ssot-smtp-policy-error",
}

RENAMES = {
    "runbook": "ops/runbook",
    "authentication": "web/authentication",
    "api-reference": "web/api-reference",
    "web-ui-architecture": "web/ui-architecture",
    "mvp-scope": "product/mvp-scope",
    "risks-legal": "product/risks-legal",
    "monitoring-plan": "product/monitoring-plan",
    "site-architecture": "product/site-architecture",
    "sort-strategy": "parser/sort-strategy",
    "local-catalog": "parser/local-catalog",
    "cabinet-free-lot": "parser/cabinet-free-lot",
    "anti-bot": "parser/anti-bot",
    "donor-site-urls": "parser/donor-site-urls",
    "server-performance": "ops/server-performance",
    "cost-estimate": "ops/cost-estimate",
    "getting-started": "ops/getting-started",
    "dev-environment": "ops/dev-environment",
}

ARCH_ANCHORS = {
    "0": "architecture/00-open-questions-resolved",
    "0.1": "architecture/00-open-questions-resolved",
    "1": "architecture/01-container-diagram",
    "2": "architecture/02-layers-dip",
    "3": "architecture/03-protocols",
    "3.1": "architecture/03-protocols",
    "3.2": "architecture/03-protocols",
    "3.3": "architecture/03-protocols",
    "3.4": "architecture/03-protocols",
    "3.5": "architecture/03-protocols",
    "3.6": "architecture/03-protocols",
    "3.6.1": "architecture/03-protocols",
    "3.6.2": "architecture/03-protocols",
    "4": "architecture/04-composition-root",
    "4.1": "architecture/04-composition-root",
    "4.2": "architecture/04-composition-root",
    "4.3": "architecture/04-composition-root",
    "4.3.bis": "architecture/04-composition-root",
    "4.4": "architecture/04-composition-root",
    "5": "architecture/05-extension-points",
    "6": "architecture/06-notifier-registry",
    "7": "architecture/07-concurrency",
    "7.1": "architecture/07-concurrency",
    "7.2": "architecture/07-concurrency",
    "7.3": "architecture/07-concurrency",
    "7.4": "architecture/07-concurrency",
    "7.5": "architecture/07-concurrency",
    "7.6": "architecture/07-concurrency",
    "8": "architecture/08-error-strategy",
    "9": "architecture/09-test-strategy",
    "10": "architecture/10-project-structure-diffs",
    "10.7": "architecture/10-7-diagnostic-zip",
    "10.8": "architecture/10-8-backup-strategy",
    "10.9": "architecture/10-9-http-logs",
    "11": "decisions-log",
}

DATA_MODEL_ANCHORS = {
    "FieldChange--LotUpsertResult": "data-model/lot",
    "LotPublicDTO--LotUserDTO": "data-model/lot",
    "ResolvedSmtpEndpoint": "data-model/notifications",
    "SsePayloadSchema": "data-model/sse",
}


def transform(text):
    def adr_repl(m):
        num = m.group(1)
        slug = ADR_MAP.get(num)
        return f"[[decisions/{slug}|ADR-{num}]]" if slug else m.group(0)

    text = re.sub(r"\[\[decisions-log#ADR-(\d{3})(?:[^\]|]*?)?\]\]", adr_repl, text)

    def arch_repl(m):
        anchor = m.group(1).strip().lstrip("§").strip()
        m2 = re.match(r"(\d+(?:\.\d+)*(?:\.bis)?)", anchor)
        if m2:
            tgt = ARCH_ANCHORS.get(m2.group(1))
            if tgt:
                return f"[[{tgt}]]"
        return "[[architecture]]"

    text = re.sub(r"\[\[architecture#([^\]|]+)\]\]", arch_repl, text)

    def dm_repl(m):
        return f"[[{DATA_MODEL_ANCHORS.get(m.group(1), 'data-model')}]]"

    text = re.sub(r"\[\[data-model#([^\]|]+)\]\]", dm_repl, text)
    text = text.replace("[[data-model.md]]", "[[data-model]]")

    for old, new in RENAMES.items():
        text = re.sub(rf"\[\[{re.escape(old)}\]\]", f"[[{new}]]", text)
        text = re.sub(rf"\[\[{re.escape(old)}\|", f"[[{new}|", text)
        text = re.sub(rf"\[\[{re.escape(old)}#", f"[[{new}#", text)

    return text


def main():
    count = 0
    for p in pathlib.Path("docs").rglob("*.md"):
        s = p.read_text(encoding="utf-8")
        s2 = transform(s)
        if s != s2:
            p.write_text(s2, encoding="utf-8")
            count += 1
            print(f"updated: {p}")
    print(f"total: {count}")


if __name__ == "__main__":
    main()
