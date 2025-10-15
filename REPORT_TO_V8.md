# What to Report to V8 Security Team

## TL;DR

I searched for **new, unknown vulnerabilities** in V8. I found **NO new exploitable memory corruption bugs**, but identified **3 security concerns** worth reporting.

---

## 🔴 PRIORITY 1: WASM Bounds Check Bypass (CRITICAL IF EXPLOITABLE)

### The Issue

There are flags that **completely disable WASM memory bounds checking**:

**File:** `src/wasm/wasm-code-manager.cc:947`
```cpp
BoundsCheckStrategy GetBoundsChecks(const WasmModule* module) {
  if (!FLAG_wasm_bounds_checks) return kNoBoundsChecks;  // ← DANGEROUS
  // ...
}
```

**File:** `src/wasm/baseline/liftoff-compiler.cc:2662`
```cpp
if (V8_UNLIKELY(env_->bounds_checks == kNoBoundsChecks)) {
  return index_ptrsize;  // ← NO BOUNDS CHECKING AT ALL!
}
```

### Why This Matters

If an attacker can control these flags (via command-line, environment variables, or any other mechanism):
- **All WASM memory access becomes unchecked**
- **Trivial out-of-bounds read/write**
- **Immediate arbitrary memory corruption**
- **Easy path to code execution**

### Questions to Ask V8 Team

1. Can `FLAG_wasm_bounds_checks` be influenced by:
   - Command-line arguments?
   - Environment variables?
   - JavaScript code?
   - Any external input?

2. Why do these flags exist in production builds?

3. Is there runtime validation that these are ONLY used in testing?

### Recommended Fix

- Remove these flags from production/release builds entirely
- If needed for testing, only compile them into debug builds
- Add `DCHECK(!production_build || FLAG_wasm_bounds_checks)` 

---

## 🟡 PRIORITY 2: CFI Disabled in Security-Critical Code

### The Issue

Control Flow Integrity is disabled in WASM memory protection key code:

**File:** `src/wasm/memory-protection-key.cc:46-58`
```cpp
// TODO(dlehmann) Security: Are there alternatives to disabling CFI altogether
// for the functions below? Since they are essentially an arbitrary indirect
// call gadget, disabling CFI should be only a last resort.
DISABLE_CFI_ICALL
int AllocateMemoryProtectionKey() {
    static auto* pkey_alloc =
        bit_cast<pkey_alloc_t>(dlsym(RTLD_DEFAULT, "pkey_alloc"));
    if (pkey_alloc != nullptr) {
        return pkey_alloc(0, kDisableAccess);  // ← Unchecked indirect call
    }
    return kNoMemoryProtectionKey;
}
```

### Why This Matters

- The TODO comment acknowledges this is suboptimal security
- Creates an indirect call gadget that bypasses CFI
- Function pointer from `dlsym()` is not integrity-checked
- Could be exploited in attack chains (e.g., corrupt the function pointer)

### Recommended Fix

1. **Best:** Implement PKU support using direct Linux syscalls instead of glibc
2. **Good:** Use protected/read-only memory for the cached function pointers
3. **Minimum:** Minimize the scope and document the security implications

---

## 🟢 PRIORITY 3: Architecture Inconsistency

### The Issue

Different architectures handle `NumberConstant` as immediate values inconsistently:

| Architecture | Behavior |
|--------------|----------|
| IA32 | Fixed to only allow ±0.0 (after Bug 1254189) |
| X64 | Only allows ±0.0 |
| RISCV64 | Only allows +0.0 (different from X64!) |
| ARM64, ARM, PPC, S390, MIPS64 | Don't handle NumberConstant at all |

### Why This Matters

- Inconsistency suggests potential for bugs
- RISCV64 differs from X64 in subtle way
- Harder to review security if each arch is different

### Recommended Fix

- Standardize NumberConstant handling across all architectures
- Document why architectures differ (if intentional)
- Add cross-architecture consistency tests

---

## How to Report

### To Chromium Bug Tracker

1. **Navigate to:** https://bugs.chromium.org/p/chromium/issues/entry?template=Security+Bug
2. **Type:** Security Bug
3. **Component:** Blink>JavaScript
4. **Title:** "WASM bounds check bypass flags in production builds"
5. **Description:** Use Priority 1 section above
6. **Mark as:** Restrict-View-SecurityTeam

### To V8 Team Directly

Email: v8-security@googlegroups.com

**Subject:** Security concerns in WASM bounds checking and CFI

**Body:**
```
Hi V8 Security Team,

While auditing the V8 codebase for memory corruption vulnerabilities, 
I identified three security concerns that warrant your attention:

1. [CRITICAL IF EXPLOITABLE] WASM bounds check bypass flags exist in 
   production builds (FLAG_wasm_bounds_checks)
   
2. [MEDIUM] CFI disabled in memory protection key allocation code
   
3. [LOW] Architecture inconsistencies in NumberConstant handling

Detailed analysis and recommendations are in the attached report.

The main question: Can FLAG_wasm_bounds_checks be influenced by an 
attacker? If yes, this is a critical vulnerability.

Best regards,
[Your name]
```

---

## What I Already Found (Don't Report These - Already Fixed)

✅ **CVE-2021-4102** - Type confusion in SimplifiedOperatorReducer (fixed)  
✅ **Bug 1254189** - IA32 NumberConstant immediate misuse (fixed)

These are documented in my other reports but are already patched in the current codebase.

---

## Summary

**New exploitable vulnerabilities:** 0  
**Security concerns to report:** 3  
**Most critical:** WASM bounds check bypass (if flags are controllable)  
**Documentation created:** 6 files with full analysis

The V8 team takes security seriously (evidence: previous bugs are fixed). These concerns are worth reporting to help them improve defense-in-depth.
