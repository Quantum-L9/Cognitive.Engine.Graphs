what are the prerequisites for a full Org-level ruleset enforcement and how to activate it?

total: 39
archived: 0
forks: 0
private: 10
public: 29
        "html": {
            "href": "http://github.com/organizations/Quantum-L9/settings/policies/repositories/18226001"
        }
    }
}
python3 << 'EOF'
import json, subprocess
prs = json.load(open("/tmp/pr_map.json"))
# add l9-tools
prs.append(("l9-tools", "1"))

summary = []
for repo, num in prs:
    r = subprocess.run(
        ["gh", "pr", "checks", num, "--repo", f"Quantum-L9/{repo}", "--json", "name,state,bucket"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        summary.append((repo, num, "API_ERROR"))
        continue
    try:
        checks = json.loads(r.stdout)
    except json.JSONDecodeError:
        summary.append((repo, num, "PARSE_ERROR"))
        continue
    total = len(checks)
    failing = [c["name"] for c in checks if c.get("bucket") == "fail"]
    pending = [c["name"] for c in checks if c.get("bucket") == "pending"]
    summary.append((repo, num, f"total={total} failing={len(failing)} pending={len(pending)}", failing))

for repo, num, stat, *rest in summary:
    fail_list = rest[0] if rest else []
    print(f"{repo:<35} #{num:<5} {stat}")
    if fail_list:
        print("    failing:", ", ".join(fail_list))
EOF



gh repo list Quantum-L9 --limit 300 --json name -q '.[].name' > /tmp/all_org_repos.txt
python3 << 'EOF'
import json
results = json.load(open("/tmp/l9_ci_rollout_results.json"))
covered = {r["repo"] for r in results} | {"l9-tools"}
all_repos = set(open("/tmp/all_org_repos.txt").read().splitlines())
not_covered = sorted(all_repos - covered)
print("total org repos:", len(all_repos))
print("covered by rollout:", len(covered))
print("NOT covered:", len(not_covered))
for r in not_covered:
    print(" -", r)
EOF
