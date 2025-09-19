#!/usr/bin/env python3
import os
import re
import json
import argparse
from typing import Dict, List, Tuple, Optional

# Extremely conservative taint analysis for C/C++ sources.
# Goal: zero (or near-zero) false positives by only reporting flows we can prove
# are unbounded copies from clearly-tainted sources directly into fixed-size
# buffers via unsafe sinks, with no length checks in between.

SOURCE_EXTENSIONS = (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh")

# Heuristics for sources and sinks
UNBOUNDED_SOURCES = [
	# C stdlib and POSIX
	"getenv(", "getenv_s(", "gets(", "fgets(", "getline(", "read(", "recv(", "recvfrom(",
	# C++
	"std::getline(",
]

UNSAFE_SINKS = [
	"strcpy(", "strcat(", "sprintf(", "vsprintf(", "gets(",
]

POTENTIALLY_UNSAFE_SINKS = [
	"memcpy(", "memmove(", "strncpy(", "strncat(",
]

DECL_FIXED_BUFFER_RE = re.compile(r"\b(char|unsigned\s+char)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*(\d+)\s*\]\s*;")
CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_:<>]*)\s*\((.*)\)")
IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")

def find_fixed_buffers(lines: List[str]) -> Dict[str, int]:
	name_to_size: Dict[str, int] = {}
	for line in lines:
		m = DECL_FIXED_BUFFER_RE.search(line)
		if m:
			_, name, size = m.groups()
			try:
				name_to_size[name] = int(size)
			except ValueError:
				pass
	return name_to_size

def split_args(argstr: str) -> List[str]:
	# Split top-level commas (not perfect, but good enough without nested parens)
	args = []
	depth = 0
	current = []
	for ch in argstr:
		if ch == '(':
			depth += 1
		elif ch == ')':
			depth = max(0, depth - 1)
		elif ch == ',' and depth == 0:
			args.append(''.join(current).strip())
			current = []
			continue
		current.append(ch)
	if current:
		args.append(''.join(current).strip())
	return args

def is_unbounded_source_expr(expr: str) -> bool:
	expr_ws = expr.replace(' ', '')
	for s in UNBOUNDED_SOURCES:
		if s.replace(' ', '') in expr_ws:
			return True
	return False

def extract_identifiers(expr: str) -> List[str]:
	return IDENT_RE.findall(expr)

def has_any_identifier(expr: str, idents: List[str]) -> bool:
	for ident in idents:
		if re.search(r"\b" + re.escape(ident) + r"\b", expr):
			return True
	return False

def analyze_file(path: str) -> List[Dict]:
	findings: List[Dict] = []
	try:
		with open(path, 'r', errors='ignore') as f:
			lines = f.readlines()
	except Exception:
		return findings

	buffer_sizes = find_fixed_buffers(lines)

	# Track current function parameters per a simple heuristic: when we see a line like
	# 'type name(type p1, type p2, ...)' followed by '{', grab p1, p2 names.
	func_param_stack: List[List[str]] = []
	func_brace_depth = 0

	FUNC_DEF_RE = re.compile(r"^[\w:\*\&\<\>\s]+\s+([A-Za-z_][A-Za-z0-9_:<>]*)\s*\(([^;]*)\)\s*\{\s*$")
	PARAM_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(=|,|\)|$)")

	for lineno, line in enumerate(lines, start=1):
		strip = line.strip()
		# Function entry/exit tracking
		mfunc = FUNC_DEF_RE.match(strip)
		if mfunc:
			param_blob = mfunc.group(2)
			params: List[str] = []
			for p in split_args(param_blob):
				pm = PARAM_NAME_RE.search(p.strip()[::-1])
				if pm:
					name_rev = pm.group(1)
					name = name_rev[::-1]
					if name and name not in ("const", "volatile"):
						params.append(name)
			func_param_stack.append(params)
			func_brace_depth = 1
			continue

		if func_param_stack:
			func_brace_depth += line.count('{') - line.count('}')
			if func_brace_depth <= 0:
				func_param_stack.pop()
				func_brace_depth = 0
				continue

		# Calls scanning
		cm = CALL_RE.search(strip)
		if not cm:
			continue
		callee = cm.group(1)
		args_str = cm.group(2)
		args = split_args(args_str)

		callee_ws = callee.replace(' ', '')
		current_params = func_param_stack[-1] if func_param_stack else []

		# High-confidence sinks: strcpy, strcat, sprintf, vsprintf, gets
		if any(callee_ws.startswith(s[:-1]) for s in UNSAFE_SINKS):
			if callee_ws.startswith('gets'):
				if args and args[0] in buffer_sizes:
					findings.append({
						"severity": "critical",
						"rule": "gets-into-fixed-buffer",
						"file": path,
						"line": lineno,
						"message": f"gets into fixed buffer '{args[0]}' of size {buffer_sizes[args[0]]}",
					})
				continue

			if callee_ws.startswith('strcpy') or callee_ws.startswith('strcat'):
				if len(args) >= 2 and args[0] in buffer_sizes:
					src = args[1]
					if (src in current_params) or is_unbounded_source_expr(src):
						findings.append({
							"severity": "critical",
							"rule": f"{callee_ws.split('(')[0]}-tainted-into-fixed-buffer",
							"file": path,
							"line": lineno,
							"message": f"{callee_ws.split('(')[0]} into fixed buffer '{args[0]}' from tainted source '{src}'",
						})
				continue

			if callee_ws.startswith('sprintf') or callee_ws.startswith('vsprintf'):
				if args and args[0] in buffer_sizes:
					fmt_expr = args[1] if len(args) > 1 else ""
					other_args = args[2:]
					if is_unbounded_source_expr(fmt_expr) or has_any_identifier(fmt_expr, current_params):
						sev = "critical"
						msg = "tainted format string"
					else:
						sev = "high" if any(is_unbounded_source_expr(a) or (a in current_params) for a in other_args) else None
						msg = "tainted argument used in formatting" if sev else None
					if sev:
						findings.append({
							"severity": sev,
							"rule": f"{callee_ws.split('(')[0]}-tainted-into-fixed-buffer",
							"file": path,
							"line": lineno,
							"message": f"{callee_ws.split('(')[0]} into fixed buffer '{args[0]}': {msg}",
						})
				continue

		# Potentially unsafe sinks: accept only obvious constant overflow cases
		if any(callee_ws.startswith(s[:-1]) for s in POTENTIALLY_UNSAFE_SINKS):
			if callee_ws.startswith('memcpy') or callee_ws.startswith('memmove'):
				if len(args) >= 3 and args[0] in buffer_sizes:
					len_arg = args[2]
					try:
						const_len = int(len_arg)
						if const_len > buffer_sizes[args[0]]:
							findings.append({
								"severity": "critical",
								"rule": "memcpy-const-overflow",
								"file": path,
								"line": lineno,
								"message": f"memcpy into fixed buffer '{args[0]}' of size {buffer_sizes[args[0]]} with length {const_len}",
							})
					except ValueError:
						pass

	return findings

def scan_tree(root: str, include_tests: bool = False) -> List[Dict]:
	findings: List[Dict] = []
	for dirpath, dirnames, filenames in os.walk(root):
		skip = any(part in (
			".git", "out", "out.gn", "out-asan", "build-asan", "third_party", "node_modules") for part in dirpath.split(os.sep))
		if skip:
			continue
		if not include_tests and ("test" in dirpath.split(os.sep) or "testing" in dirpath.split(os.sep)):
			continue
		for fn in filenames:
			if fn.endswith(SOURCE_EXTENSIONS):
				path = os.path.join(dirpath, fn)
				findings.extend(analyze_file(path))
	return findings

def severity_order(sev: str) -> int:
	return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(sev, 4)

def main():
	parser = argparse.ArgumentParser(description="Conservative taint-based memory-corruption scanner")
	parser.add_argument("--root", default="/workspace", help="Project root to scan")
	parser.add_argument("--include-tests", action="store_true", help="Also scan tests/")
	parser.add_argument("--json", dest="json_out", default="", help="Write findings as JSON to this path")
	parser.add_argument("--min-severity", default="low", choices=["critical", "high", "medium", "low"], help="Minimum severity to report")
	args = parser.parse_args()

	findings = scan_tree(args.root, include_tests=args.include_tests)
	threshold = severity_order(args.min_severity)
	findings = [f for f in findings if severity_order(f["severity"]) <= threshold]
	findings.sort(key=lambda f: (severity_order(f["severity"]), f["file"], f["line"]))

	print(f"Findings: {len(findings)}")
	for f in findings:
		print(f"[{f['severity'].upper():8}] {f['file']}:{f['line']}: {f['rule']} - {f['message']}")

	if args.json_out:
		with open(args.json_out, 'w') as jf:
			json.dump({"findings": findings}, jf, indent=2)
			print(f"\nWrote JSON report to {args.json_out}")

if __name__ == "__main__":
	main()

