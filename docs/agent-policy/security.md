# Policy security
The canonical policy is `AGENTS.md`. Supporting documents remain local to the
repository. The loader never fetches policy text from the network.

The loader rejects missing, malformed, non-ASCII, oversized, symlinked, and
special files. It rejects absolute paths and paths that escape the policy root.
It assembles deterministic output and fails closed.

Do not place secrets, credentials, tokens, private keys, or sensitive
vulnerability details in policy documents, examples, logs, handoffs, or
generated copies.

Secret categories include keys, tokens, passwords, private keys, and `.env`
files. If a secret enters version control, stop committing and recommend
rotation. The secret checker uses heuristics. It does not perform entropy
analysis or prove that a repository contains no secret.

Use bcrypt, scrypt, or Argon2 with salt and work factor for passwords. Use
SHA-256 or SHA-3 for general hashing. Never use MD5 or SHA-1 for security.
Example cache use: `hashlib.md5(payload).hexdigest()` with a comment stating
that the digest is non-cryptographic.

A comment cannot convert a security-sensitive use into a non-security use.

For runtime-root containers, prefer ports of 1024 or higher behind a reverse
proxy or port mapping. Prefer `COPY --chown` or build-time `chown`. Set
`user:` in Compose. Set `securityContext.runAsNonRoot: true` and `runAsUser`
in Kubernetes. After approval, add:

`# runtime-root: this container <reason> (Rule 12 exception).`

Flag unrelated runtime-root findings instead of fixing them under Rule 4.
`scripts/check_dockerfile_root.py` checks the rule.

Preserve license-required attribution and source metadata in every controlled
adoption and redistribution. Third-party mirrors remain responsible for their
own legal compliance. This repository cannot enforce or verify their local
practices.

Report vulnerabilities through the process in `SECURITY.md.example`. Do not
use public issues or pull requests for private vulnerability details.

`AGENTS.md` controls when linked documents conflict with it.

Avoid nested quantifiers such as `(x+)+` and overlapping patterns. Use atomic
groups, possessive quantifiers, or simpler expressions.

Git transport over SSH is allowed through Git commands. Direct SSH client
execution remains denied. Shell gates deny protected commands and paths. The
Claude file-tool gate denies protected file operations and broad searches.
Other clients may lack equivalent file-tool coverage. External controls remain
necessary for tamper resistance.

Injection examples:

- Bad: `cursor.execute(f"SELECT * FROM users WHERE name = '{name}'")`
- Good: `cursor.execute("SELECT * FROM users WHERE name = %s", (name,))`
- Bad: `subprocess.run(f"convert {filename} out.png", shell=True)`
- Good: `subprocess.run(["convert", filename, "out.png"])`

## Denied command families
The denial covers AWS CLI, SAM, CDK, Azure CLI and PowerShell, Google Cloud
CLI, `gsutil`, `bq`, Terraform, OpenTofu, Terragrunt, Pulumi, Packer,
Kubernetes, Helm, Kustomize, OpenShift, Minikube, Kind, SSH clients, PuTTY,
FTP, TFTP, Telnet, iptables, nftables, UFW, firewalld, and Windows firewall
commands.

Cloudflare Pages deployment is a limited exception to the cloud-tool denial.
Permit a local build and `wrangler pages deploy <workspace-path>
--project-name <name>` with an optional literal `--branch <branch>`. Require
the deployment path to resolve inside the workspace and require an explicit
project name. Deny every other Wrangler operation, including Workers,
account, zone, DNS, WAF, Turnstile, KV, D1, R2, Queues, Durable Objects,
secret, configuration, inspection, and API operations. Keep dashboard
automation and infrastructure-as-code denied.
Require the path to target a dedicated non-hidden output directory named
`build` or `dist`. Reject the workspace root, hidden directories, protected
credential names, and outputs containing `.env` or protected credential files.

Protected content includes AWS, Azure, Google Cloud, SSH, Kubernetes,
Terraform, FTP, and Netrc credentials, Terraform source, variables, state,
locks and CLI configuration, plus Kubernetes, Helm, and Kustomize manifests
and project directories.
