# 🔴 POTENTIAL NEW VULNERABILITY: WASM Exception Value Corruption

## Executive Summary

**FOUND:** Potential sign extension bug in WASM exception handling that corrupts 32-bit values during decode.

**Status:** ⚠️ **NEEDS VERIFICATION** - Requires testing to confirm exploitability  
**Severity:** Medium-High (Data corruption in WASM exceptions)  
**Impact:** Incorrect exception values, potential type confusion

---

## The Vulnerability

### Location
**File:** `src/compiler/wasm-compiler.cc`  
**Function:** `BuildDecodeException32BitValue`  
**Lines:** 2452-2463

### Vulnerable Code

```cpp
Node* WasmGraphBuilder::BuildDecodeException32BitValue(Node* values_array,
                                                       uint32_t* index) {
  Node* upper = BuildChangeSmiToInt32(  // ← Uses SIGNED conversion
      gasm_->LoadFixedArrayElementSmi(values_array, *index));
  (*index)++;
  upper = gasm_->Word32Shl(upper, Int32Constant(16));
  
  Node* lower = BuildChangeSmiToInt32(  // ← BUG: SIGNED conversion of unsigned data!
      gasm_->LoadFixedArrayElementSmi(values_array, *index));
  (*index)++;
  
  Node* value = gasm_->Word32Or(upper, lower);  // ← Corrupted value
  return value;
}
```

### The Encoding Side (Correct)

```cpp
void WasmGraphBuilder::BuildEncodeException32BitValue(Node* values_array,
                                                      uint32_t* index,
                                                      Node* value) {
  Node* upper_halfword_as_smi =
      BuildChangeUint31ToSmi(gasm_->Word32Shr(value, Int32Constant(16)));  // ← UNSIGNED
  gasm_->StoreFixedArrayElementSmi(values_array, *index, upper_halfword_as_smi);
  ++(*index);
  
  Node* lower_halfword_as_smi =
      BuildChangeUint31ToSmi(gasm_->Word32And(value, Int32Constant(0xFFFFu)));  // ← UNSIGNED
  gasm_->StoreFixedArrayElementSmi(values_array, *index, lower_halfword_as_smi);
  ++(*index);
}
```

---

## Root Cause Analysis

### The Problem

1. **Encoding uses UNSIGNED conversion:**
   - `BuildChangeUint31ToSmi()` - treats 16-bit values as unsigned
   
2. **Decoding uses SIGNED conversion:**
   - `BuildChangeSmiToInt32()` - performs **Word32Sar** (arithmetic right shift)
   - This causes sign extension!

3. **BuildChangeSmiToInt32 Implementation:**
```cpp
Node* WasmGraphBuilder::BuildChangeSmiToInt32(Node* value) {
  return COMPRESS_POINTERS_BOOL
             ? gasm_->Word32Sar(value, BuildSmiShiftBitsConstant32())  // ← SAR = sign extend!
             : BuildTruncateIntPtrToInt32(
                   gasm_->WordSar(value, BuildSmiShiftBitsConstant()));  // ← SAR = sign extend!
}
```

---

## Exploitation Scenario

### Example 1: Value with High Bit Set in Lower Half

```
Original 32-bit value: 0x12348000

ENCODE:
  upper = (0x12348000 >> 16) = 0x1234  → stored as Smi
  lower = (0x12348000 & 0xFFFF) = 0x8000  → stored as Smi

DECODE:
  upper_smi = load Smi(0x1234)
  upper = SmiToInt32(upper_smi) = 0x1234
  upper = 0x1234 << 16 = 0x12340000  ✓ correct
  
  lower_smi = load Smi(0x8000)
  lower = SmiToInt32(lower_smi)
        = Word32Sar(0x8000 << shift, shift)
        = 0xFFFF8000  ✗ WRONG! Sign extended!
  
  result = 0x12340000 | 0xFFFF8000 
         = 0xFFFF8000  ✗ WRONG!

EXPECTED: 0x12348000
GOT:      0xFFFF8000
```

### Example 2: Maximum Lower Half Value

```
Original: 0x0000FFFF

ENCODE:
  upper = 0x0000
  lower = 0xFFFF

DECODE:
  upper = 0x0000 << 16 = 0x00000000
  lower = SmiToInt32(0xFFFF) = 0xFFFFFFFF  ← Sign extended!
  result = 0x00000000 | 0xFFFFFFFF = 0xFFFFFFFF

EXPECTED: 0x0000FFFF
GOT:      0xFFFFFFFF
```

---

## Impact Assessment

### Direct Impact

1. **Data Corruption:**
   - Any 32-bit exception value with bit 15 set in lower half gets corrupted
   - Affects approximately 50% of possible values (0x8000-0xFFFF in lower half)

2. **WASM Exception Values:**
   - i32 values thrown/caught in WASM exceptions
   - f32 values (bitcast to i32) 
   - Addresses/pointers (if exception contains them)

3. **Type Confusion:**
   - If exception value is used for:
     - Array indexing → potential OOB access
     - Memory addresses → wrong memory access
     - Control flow decisions → wrong branch taken

### Security Implications

**Scenario 1: Exception-based Type Confusion**
```javascript
// WASM throws exception with value 0x00008000
// Attacker expects this value for array access
// After corruption, value becomes 0xFFFF8000 (negative!)
// Can cause out-of-bounds access with unexpected offset
```

**Scenario 2: Information Disclosure**
```javascript
// Exception contains pointer/address in lower 16 bits
// Corruption changes the value
// Could leak different memory contents than intended
```

**Scenario 3: Integer Confusion**
```javascript
// f32 value bitcast to i32 in exception
// After corruption, completely different float value
// Could bypass validation checks
```

---

## Verification Steps

### Test Case 1: Basic Corruption
```wat
(module
  (tag $t (param i32))
  (func (export "test")
    ;; Throw exception with value 0x12348000
    (throw $t (i32.const 0x12348000))
  )
  (func (export "catch_test") (result i32)
    (try (result i32)
      (do (call 0) (i32.const 0))
      (catch $t 
        ;; Should return 0x12348000
        ;; Bug would return 0xFFFF8000
      )
    )
  )
)
```

### Test Case 2: Edge Case Values
```wat
;; Test these values:
0x00008000  → Should be 0x00008000, bug gives 0xFFFF8000
0x0000FFFF  → Should be 0x0000FFFF, bug gives 0xFFFFFFFF
0x12348FFF  → Should be 0x12348FFF, bug gives 0xFFFF8FFF
```

---

## Proposed Fix

### Option 1: Use Unsigned Conversion (Recommended)

```cpp
Node* WasmGraphBuilder::BuildDecodeException32BitValue(Node* values_array,
                                                       uint32_t* index) {
  Node* upper = BuildChangeSmiToInt32(
      gasm_->LoadFixedArrayElementSmi(values_array, *index));
  (*index)++;
  upper = gasm_->Word32Shl(upper, Int32Constant(16));
  
  // FIX: Use unsigned conversion for lower bits
  Node* lower_smi = gasm_->LoadFixedArrayElementSmi(values_array, *index);
  Node* lower = gasm_->Word32Shr(  // Use SHR instead of SAR
      COMPRESS_POINTERS_BOOL ? lower_smi
                            : BuildTruncateIntPtrToInt32(lower_smi),
      BuildSmiShiftBitsConstant32());
  (*index)++;
  
  Node* value = gasm_->Word32Or(upper, lower);
  return value;
}
```

### Option 2: Mask Lower Bits

```cpp
Node* lower = BuildChangeSmiToInt32(
    gasm_->LoadFixedArrayElementSmi(values_array, *index));
lower = gasm_->Word32And(lower, Int32Constant(0xFFFF));  // Mask off sign extension
(*index)++;
```

---

## Why This Wasn't Caught

1. **Rare Test Coverage:**
   - WASM exception handling is a newer feature
   - Tests may not cover values with high bit set

2. **Subtle Sign Extension:**
   - Bug only affects specific value ranges
   - May pass basic tests with small values

3. **No Type Checking:**
   - No static check that encode/decode use matching signedness
   - Easy to miss in code review

4. **Platform-Dependent:**
   - Behavior may vary with pointer compression settings
   - Different Smi representations on 32-bit vs 64-bit

---

## Recommended Actions

### Immediate

1. **Verify the bug exists:**
   - Write test case with value 0x00008000
   - Confirm corruption occurs

2. **Assess impact:**
   - Check if this affects production WASM code
   - Determine if it's exploitable for security issues

3. **Prepare patch:**
   - Implement Option 1 (unsigned conversion)
   - Add regression tests

### Long-term

1. **Audit all Smi conversions:**
   - Check for similar sign extension issues
   - Standardize signed vs unsigned conversions

2. **Add static analysis:**
   - Detect mismatched encode/decode signedness
   - Flag SAR operations on unsigned data

3. **Improve test coverage:**
   - Test all edge cases for exception values
   - Include values with high bits set

---

## Severity Assessment

**Likelihood:** Medium (affects 50% of value space, but exceptions are not always used)  
**Impact:** Medium-High (data corruption, potential type confusion)  
**Exploitability:** Medium (requires specific exception values and usage patterns)  
**Overall Severity:** **MEDIUM-HIGH**

**CVE Candidate:** Yes, if verification confirms exploitable impact

---

## References

- **Encode Function:** `BuildEncodeException32BitValue` (line 2439)
- **Decode Function:** `BuildDecodeException32BitValue` (line 2452)
- **Smi Conversion:** `BuildChangeSmiToInt32` (line 3383)
- **Used In:** WASM exception encoding for i32, f32, i64, f64, s128 values

---

## Next Steps for Reporter

1. Create minimal WASM test case
2. Verify corruption occurs
3. Test if corruption is exploitable
4. Report to V8 security team with:
   - This analysis
   - Proof-of-concept WASM module
   - Suggested fix
   - Severity assessment
