# Eval label review packet: ortho_pain_v1

Version: v1 snapshot, generated 2026-07-09 at commit 72728d5. System outputs are from the live run of `medilens evaluate` on the same date (model claude-sonnet-5, prompt validation_v3).

> Note: the eval set grew after this snapshot was generated. Three cases are not covered by the case walkthroughs below and should be adjudicated from the current `ortho_pain_v1.yaml`: `knee-injection-supported-medicare` (knee major-joint injection, code_in_set coverage), `shoulder-injection-no-policy-medicare` (expected refusal, no governing policy), and `lumbar-mri-payerb-short-duration` (same note as case 1 under National Commercial Payer B's stricter 12-week duration, expected does_not_meet). Regenerate this packet before the review session so every current case has a walkthrough.

## Purpose and scope

The evaluation gold labels were written by the developers as placeholders and have not been reviewed by a certified coder. This packet exists so a certified coder can adjudicate them case by case. Until that happens, no metric from `medilens evaluate` is a real accuracy claim.

Things to know before starting:

- All notes are SYNTHETIC. No real patients, no PHI. Judge them as written, the way you would audit a real chart.
- The payer policies are SYNTHETIC illustrations (see `src/medilens/policy/seed/payer_policies_ortho_pain.yaml`). You are adjudicating whether the labels are right GIVEN those policies, not whether the policies match any real payer.
- The diagnosis-code candidate set is currently 25 ortho/pain ICD-10-CM codes (see `src/medilens/knowledge/seed/icd10cm_ortho_pain.yaml`). If the correct code for a note is not in that set, record that as a seed gap rather than forcing a second-best label.
- Determinations and clause statuses follow fail-closed rules: silence is never satisfied, and clauses needing claims history (imaging recency, frequency limits) defer to manual_review in this deployment.
- To record a decision: edit `src/medilens/eval/cases/ortho_pain_v1.yaml` (field meanings in `src/medilens/eval/README.md`), update label_rationale with your reasoning, and commit. Or mark up this document and hand it back.

## Standard checklist (every case)

- Are the expected codes the ones you would accept for this note as written? (Not what the note implies; what it documents.)
- Is the expected determination right, given the policy criteria shown in the system output and the fail-closed rules (silence is never satisfied; history lookbacks defer to manual review)?
- Are the targeted clause statuses right?
- Is the label_rationale accurate, or does it need correcting?

---

## Case 1: lumbar-mri-supported-medicare

- Requested service: lumbar MRI without contrast
- Date of service: 2026-06-01
- Payer: Medicare

### Note (synthetic)

```
SYNTHETIC NOTE. Not a real patient. Evaluation fixture only.

Chief complaint: Low back pain radiating to the left leg, 8 weeks duration.

History of present illness: Gradual onset low back pain radiating into the
left posterior thigh and calf. Worse with prolonged sitting and forward
flexion, partially relieved by rest. Denies saddle anesthesia, bowel or
bladder dysfunction, fever, or unexplained weight loss.

Conservative care to date: Completed 6 weeks of physical therapy with minimal
improvement. Naproxen with partial relief. No prior epidural steroid injection.

Physical exam: Positive straight leg raise on the left at 40 degrees.
Diminished sensation in the left L5 dermatome. Strength 4/5 left extensor
hallucis longus. Reflexes symmetric.

Assessment: Lumbar radiculopathy, left L5 distribution, persistent despite 6
weeks of conservative therapy.

Imaging history: No prior lumbar MRI or other advanced imaging has been
performed for this episode.

Plan: Order lumbar MRI without contrast to evaluate for disc herniation or
nerve root compression given failure of conservative management.
```

### Current gold labels (placeholder, to adjudicate)

- expected_codes: M54.16
- expected_determination: manual_review
- expected_denied: null (excluded from denial metrics: refusal or manual_review)
- expected_clause_statuses:
    - symptom_duration: satisfied
    - conservative_therapy: satisfied
    - objective_findings: satisfied
    - not_recent_duplicate: manual_review

Label rationale on file:

> Well-documented radiculopathy: duration, completed conservative therapy, and objective findings are all documented, so those clauses are satisfied. The imaging-recency lookback requires claims history that no source provides, so it defers to manual review and the overall determination is manual_review. This is the proof case that a note without a red flag cannot silently pass the lookback.

### System output (live run 2026-07-09)

- determination: manual_review
- denial risk score: 0.50
- recommended codes: M54.16

### Questions for the reviewer

1. Standard checklist above.

### Adjudication (to be completed by the reviewer)

- Codes accepted or revised: 
- Determination accepted or revised: 
- Clause statuses accepted or revised: 
- Notes: 
- Reviewer name and credential: 
- Date: 

---

## Case 2: lumbar-esi-supported-commercial

- Requested service: lumbar epidural steroid injection
- Date of service: 2026-06-01
- Payer: National Commercial Payer A

### Note (synthetic)

```
SYNTHETIC NOTE. Not a real patient. Evaluation fixture only.

Chief complaint: Persistent left leg radicular pain despite conservative care;
here to discuss epidural steroid injection.

History of present illness: 10 weeks of low back pain radiating down the left
posterior leg to the ankle in an L5 distribution, rated 7/10 at worst,
interfering with sleep. Denies saddle anesthesia, bowel or bladder
dysfunction, fever, or weight loss.

Conservative care to date: Completed 8 weeks of physical therapy with only
transient relief. Naproxen and a short course of oral steroids with partial,
temporary improvement. No prior epidural steroid injection.

Imaging: MRI lumbar spine shows a left paracentral disc protrusion at L4-L5
contacting the traversing left L5 nerve root, with mild left lateral recess
narrowing.

Physical exam: Positive straight leg raise on the left at 45 degrees.
Diminished sensation over the left L5 dermatome. Strength 4+/5 left extensor
hallucis longus. No long tract signs.

Assessment: Left L5 lumbar radiculopathy secondary to L4-L5 disc protrusion
confirmed on MRI, persistent despite 8 weeks of conservative therapy.

Plan: Recommend left L4-L5 transforaminal epidural steroid injection. Baseline
pain 7/10 with standing tolerance of 20 minutes documented for response
tracking. No active infection and no anticoagulation; no contraindication
identified.
```

### Current gold labels (placeholder, to adjudicate)

- expected_codes: M51.16
- expected_determination: meets_criteria
- expected_denied: false
- expected_clause_statuses:
    - radicular_imaging_correlation: satisfied
    - conservative_therapy: satisfied
    - baseline_function: satisfied
    - prior_injection_response: satisfied
    - contraindication_absent: satisfied

Label rationale on file:

> MRI confirms a disc protrusion causing L5 radiculopathy, so M51.16 is more specific than a radiculopathy-only code. All five ESI clauses are documented (correlation, 8 weeks of therapy, pain and function baseline, first injection stated, contraindications addressed), so the determination is meets_criteria and denial is not expected.

### System output (live run 2026-07-09)

- determination: meets_criteria
- denial risk score: 0.15
- recommended codes: M51.16

### Questions for the reviewer

1. Confirm M51.16 over M54.16: the disc protrusion is imaging-confirmed and named as the cause of the radiculopathy in the assessment. Is the combination code the right call as documented?

### Adjudication (to be completed by the reviewer)

- Codes accepted or revised: 
- Determination accepted or revised: 
- Clause statuses accepted or revised: 
- Notes: 
- Reviewer name and credential: 
- Date: 

---

## Case 3: lumbar-mri-underdocumented-medicare

- Requested service: lumbar MRI without contrast
- Date of service: 2026-06-01
- Payer: Medicare

### Note (synthetic)

```
SYNTHETIC NOTE. Not a real patient. Evaluation fixture only.

Chief complaint: Low back pain with left leg symptoms.

History of present illness: Patient reports ongoing low back pain with some
radiation into the left leg. Requests advanced imaging.

Assessment: Lumbar radiculopathy, left leg.

Plan: Order lumbar MRI without contrast.
```

### Current gold labels (placeholder, to adjudicate)

- expected_codes: M54.16
- expected_determination: manual_review
- expected_denied: null (excluded from denial metrics: refusal or manual_review)
- expected_clause_statuses:
    - symptom_duration: insufficient_documentation
    - conservative_therapy: insufficient_documentation
    - objective_findings: insufficient_documentation
    - not_recent_duplicate: manual_review

Label rationale on file:

> The assessment states lumbar radiculopathy so M54.16 may still be documentation-supported, but the note omits duration, conservative therapy, and exam findings: each of those clauses fails closed to insufficient_documentation. The lookback additionally defers to manual review, which outranks insufficient in the determination precedence. The per-clause expectations are the real target of this case.

### System output (live run 2026-07-09)

- determination: manual_review
- denial risk score: 0.50
- recommended codes: M54.16

### Questions for the reviewer

1. Standard checklist above.

### Adjudication (to be completed by the reviewer)

- Codes accepted or revised: 
- Determination accepted or revised: 
- Clause statuses accepted or revised: 
- Notes: 
- Reviewer name and credential: 
- Date: 

---

## Case 4: lumbar-mri-short-duration-medicare

- Requested service: lumbar MRI without contrast
- Date of service: 2026-06-01
- Payer: Medicare

### Note (synthetic)

```
SYNTHETIC NOTE. Not a real patient. Evaluation fixture only.

Chief complaint: Low back pain radiating to the left leg, 3 weeks duration.

History of present illness: Acute onset low back pain radiating into the left
posterior thigh, 3 weeks ago after lifting. Denies saddle anesthesia, bowel or
bladder dysfunction, fever, or unexplained weight loss.

Conservative care to date: Completed 2 weeks of physical therapy so far and
taking naproxen with partial relief.

Physical exam: Positive straight leg raise on the left at 50 degrees.
Diminished sensation in the left L5 dermatome. Strength 5/5 throughout.

Assessment: Lumbar radiculopathy, left L5 distribution, of 3 weeks duration.

Imaging history: No prior lumbar MRI or other advanced imaging has been
performed for this episode.

Plan: Order lumbar MRI without contrast.
```

### Current gold labels (placeholder, to adjudicate)

- expected_codes: M54.16
- expected_determination: does_not_meet
- expected_denied: true
- expected_clause_statuses:
    - symptom_duration: not_satisfied

Label rationale on file:

> Duration is documented as 3 weeks against a 6-week minimum: the deterministic rule fails affirmatively (not_satisfied), which produces does_not_meet regardless of the lookback, and a denial is expected as documented. Targets the rule engine failing a clause on verified evidence, not on silence.

### System output (live run 2026-07-09)

- determination: does_not_meet
- denial risk score: 0.85
- recommended codes: M54.16

### Questions for the reviewer

1. Standard checklist above.

### Adjudication (to be completed by the reviewer)

- Codes accepted or revised: 
- Determination accepted or revised: 
- Clause statuses accepted or revised: 
- Notes: 
- Reviewer name and credential: 
- Date: 

---

## Case 5: lumbar-mri-no-therapy-medicare

- Requested service: lumbar MRI without contrast
- Date of service: 2026-06-01
- Payer: Medicare

### Note (synthetic)

```
SYNTHETIC NOTE. Not a real patient. Evaluation fixture only.

Chief complaint: Low back pain radiating to the left leg, 8 weeks duration.

History of present illness: Gradual onset low back pain radiating into the
left posterior thigh and calf over the past 8 weeks. Worse with prolonged
sitting. Denies saddle anesthesia, bowel or bladder dysfunction, fever, or
unexplained weight loss.

Physical exam: Positive straight leg raise on the left at 40 degrees.
Diminished sensation in the left L5 dermatome. Strength 4/5 left extensor
hallucis longus.

Assessment: Lumbar radiculopathy, left L5 distribution, 8 weeks duration.

Plan: Order lumbar MRI without contrast to evaluate for disc herniation.
```

### Current gold labels (placeholder, to adjudicate)

- expected_codes: M54.16
- expected_determination: manual_review
- expected_denied: null (excluded from denial metrics: refusal or manual_review)
- expected_clause_statuses:
    - symptom_duration: satisfied
    - conservative_therapy: insufficient_documentation
    - objective_findings: satisfied

Label rationale on file:

> Duration and findings are documented but the note says nothing about conservative therapy: that clause must fail closed to insufficient_documentation (silence is never satisfied). Overall is manual_review because the lookback defers. Targets the conservative_therapy clause specifically.

### System output (live run 2026-07-09)

- determination: manual_review
- denial risk score: 0.50
- recommended codes: M54.16

### Questions for the reviewer

1. Standard checklist above.

### Adjudication (to be completed by the reviewer)

- Codes accepted or revised: 
- Determination accepted or revised: 
- Clause statuses accepted or revised: 
- Notes: 
- Reviewer name and credential: 
- Date: 

---

## Case 6: lumbar-mri-no-findings-medicare

- Requested service: lumbar MRI without contrast
- Date of service: 2026-06-01
- Payer: Medicare

### Note (synthetic)

```
SYNTHETIC NOTE. Not a real patient. Evaluation fixture only.

Chief complaint: Low back pain radiating to the left leg, 8 weeks duration.

History of present illness: Gradual onset low back pain radiating into the
left posterior thigh over the past 8 weeks. Denies saddle anesthesia, bowel
or bladder dysfunction, fever, or unexplained weight loss.

Conservative care to date: Completed 6 weeks of physical therapy with minimal
improvement. Naproxen with partial relief.

Assessment: Lumbar radiculopathy, left leg, 8 weeks duration.

Plan: Order lumbar MRI without contrast.
```

### Current gold labels (placeholder, to adjudicate)

- expected_codes: M54.16
- expected_determination: manual_review
- expected_denied: null (excluded from denial metrics: refusal or manual_review)
- expected_clause_statuses:
    - symptom_duration: satisfied
    - conservative_therapy: satisfied
    - objective_findings: insufficient_documentation

Label rationale on file:

> Duration and therapy are documented but there is no physical exam and no explanation of its absence: objective_findings fails closed. Overall is manual_review because the lookback defers. Targets the objective_findings clause specifically.

### System output (live run 2026-07-09)

- determination: manual_review
- denial risk score: 0.50
- recommended codes: M54.16

### Questions for the reviewer

1. Standard checklist above.

### Adjudication (to be completed by the reviewer)

- Codes accepted or revised: 
- Determination accepted or revised: 
- Clause statuses accepted or revised: 
- Notes: 
- Reviewer name and credential: 
- Date: 

---

## Case 7: lumbar-mri-red-flag-medicare

- Requested service: lumbar MRI without contrast
- Date of service: 2026-06-01
- Payer: Medicare

### Note (synthetic)

```
SYNTHETIC NOTE. Not a real patient. Evaluation fixture only.

Chief complaint: Acute severe low back pain with new bilateral leg weakness,
onset 2 days ago.

History of present illness: Sudden onset severe low back pain 2 days ago with
progressive bilateral lower extremity weakness, new saddle anesthesia, and
one episode of urinary retention this morning. Suspected cauda equina
syndrome.

Physical exam: Diminished perianal sensation. Bilateral lower extremity
weakness 3/5. Diminished rectal tone.

Assessment: Suspected cauda equina syndrome. Emergent evaluation indicated.

Plan: Emergent lumbar MRI without contrast now. Neurosurgery notified.
```

### Current gold labels (placeholder, to adjudicate)

- expected_codes: M54.16
- expected_determination: meets_criteria
- expected_denied: false
- expected_clause_statuses:
    - red_flag: satisfied
    - symptom_duration: not_applicable
    - conservative_therapy: not_applicable
    - objective_findings: not_applicable
    - not_recent_duplicate: not_applicable

Label rationale on file:

> Suspected cauda equina syndrome is documented: the red-flag override resolves satisfied with verified evidence and bypasses the entire gating prerequisite set, including the imaging-recency lookback (the emergency moots it; bypass renders clauses not applicable, it never asserts they passed). Overall is meets_criteria: image now. Targets the policy-level override behavior.

### System output (live run 2026-07-09)

- determination: meets_criteria
- denial risk score: 0.15
- recommended codes: M54.50
- CODE MISMATCH vs gold label: this case needs your call.

### Questions for the reviewer

1. KNOWN DISAGREEMENT (code): the gold label expects M54.16 (radiculopathy, lumbar region) but the system recommends M54.50 (low back pain, unspecified). The note documents suspected cauda equina syndrome with progressive bilateral leg weakness and saddle anesthesia, but the assessment does not name radiculopathy. Which code (or codes) would you accept for this note as written? If the answer is 'the note should be coded for the cauda equina picture and the current candidate set has no adequate code', say so: that is a code-seed gap, not a labeling gap.

### Adjudication (to be completed by the reviewer)

- Codes accepted or revised: 
- Determination accepted or revised: 
- Clause statuses accepted or revised: 
- Notes: 
- Reviewer name and credential: 
- Date: 

---

## Case 8: lumbar-esi-contradictory-commercial

- Requested service: lumbar epidural steroid injection
- Date of service: 2026-06-01
- Payer: National Commercial Payer A

### Note (synthetic)

```
SYNTHETIC NOTE. Not a real patient. Evaluation fixture only.

Chief complaint: Persistent left leg radicular pain; here to discuss epidural
steroid injection.

History of present illness: 12 weeks of low back pain radiating down the left
posterior leg in an L5 distribution, rated 7/10 at worst. Denies saddle
anesthesia, bowel or bladder dysfunction, fever, or weight loss.

Conservative care to date: Completed 8 weeks of physical therapy with only
transient relief. Naproxen with partial improvement. No prior epidural
steroid injection.

Imaging: MRI lumbar spine shows a left paracentral disc protrusion at L4-L5
contacting the traversing left L5 nerve root.

Physical exam: Positive straight leg raise on the left at 45 degrees.
Diminished sensation over the left L5 dermatome.

Baseline: Current pain 7/10 with standing tolerance of 20 minutes documented
for response tracking. No active infection and no anticoagulation; no
contraindication identified.

Assessment: Left L5 lumbar radiculopathy secondary to L4-L5 disc protrusion.

Plan: Proceed with left L4-L5 transforaminal epidural steroid injection. This
will be her fourth epidural steroid injection this year.
```

### Current gold labels (placeholder, to adjudicate)

- expected_codes: M51.16
- expected_determination: does_not_meet
- expected_denied: true
- expected_clause_statuses:
    - prior_injection_response: contradictory_documentation

Label rationale on file:

> The note states both "No prior epidural steroid injection" and "This will be her fourth epidural steroid injection this year": verified evidence points both ways on the prior-injection clause, which is contradictory_documentation and fails closed to does_not_meet. Targets contradiction detection with cited conflicting spans.

### System output (live run 2026-07-09)

- determination: does_not_meet
- denial risk score: 0.85
- recommended codes: M51.16

### Questions for the reviewer

1. Standard checklist above.

### Adjudication (to be completed by the reviewer)

- Codes accepted or revised: 
- Determination accepted or revised: 
- Clause statuses accepted or revised: 
- Notes: 
- Reviewer name and credential: 
- Date: 

---

## Case 9: lumbar-esi-no-baseline-commercial

- Requested service: lumbar epidural steroid injection
- Date of service: 2026-06-01
- Payer: National Commercial Payer A

### Note (synthetic)

```
SYNTHETIC NOTE. Not a real patient. Evaluation fixture only.

Chief complaint: Persistent left leg radicular pain despite conservative care;
here to discuss epidural steroid injection.

History of present illness: 10 weeks of low back pain radiating down the left
posterior leg to the ankle in an L5 distribution. Denies saddle anesthesia,
bowel or bladder dysfunction, fever, or weight loss.

Conservative care to date: Completed 8 weeks of physical therapy with only
transient relief. Naproxen with partial, temporary improvement. No prior
epidural steroid injection.

Imaging: MRI lumbar spine shows a left paracentral disc protrusion at L4-L5
contacting the traversing left L5 nerve root.

Physical exam: Positive straight leg raise on the left at 45 degrees.
Diminished sensation over the left L5 dermatome.

Assessment: Left L5 lumbar radiculopathy secondary to L4-L5 disc protrusion
confirmed on MRI, persistent despite 8 weeks of conservative therapy.

Plan: Recommend left L4-L5 transforaminal epidural steroid injection. No
active infection and no anticoagulation; no contraindication identified.
```

### Current gold labels (placeholder, to adjudicate)

- expected_codes: M51.16
- expected_determination: insufficient_documentation
- expected_denied: true
- expected_clause_statuses:
    - baseline_function: insufficient_documentation
    - radicular_imaging_correlation: satisfied
    - conservative_therapy: satisfied

Label rationale on file:

> Everything is documented except a pain level and functional baseline for measuring response: baseline_function fails closed to insufficient_documentation and the overall determination is insufficient_documentation (the ESI policy has no manual-review clause). As documented a denial is expected. This is the case that exercises the insufficient band of the computed score.

### System output (live run 2026-07-09)

- determination: insufficient_documentation
- denial risk score: 0.41
- recommended codes: M51.16

### Questions for the reviewer

1. Standard checklist above.

### Adjudication (to be completed by the reviewer)

- Codes accepted or revised: 
- Determination accepted or revised: 
- Clause statuses accepted or revised: 
- Notes: 
- Reviewer name and credential: 
- Date: 

---

## Case 10: lumbar-rfa-first-medicare

- Requested service: radiofrequency ablation, lumbar facet
- Date of service: 2026-06-01
- Payer: Medicare

### Note (synthetic)

```
SYNTHETIC NOTE. Not a real patient. Evaluation fixture only.

Chief complaint: Chronic axial low back pain; here to discuss radiofrequency
ablation of the lumbar facets.

History of present illness: 8 months of chronic axial low back pain, worse
with extension and rotation, without radicular features. Denies saddle
anesthesia, bowel or bladder dysfunction, fever, or weight loss.

Prior interventions: Diagnostic left L4-L5 medial branch block performed 3
weeks ago with 80 percent relief of the index pain for the duration of the
local anesthetic, documented on the post-procedure pain log.

Physical exam: Paraspinal tenderness over the left L4-L5 facets. Pain with
extension and rotation. Neurologically intact.

Assessment: Lumbar facet-mediated pain, left L4-L5, confirmed by positive
diagnostic medial branch block.

Plan: Proceed with left L4-L5 radiofrequency ablation of the medial branches.
```

### Current gold labels (placeholder, to adjudicate)

- expected_codes: (none)
- expected_determination: manual_review
- expected_denied: null (excluded from denial metrics: refusal or manual_review)
- expected_clause_statuses:
    - diagnostic_block: satisfied
    - frequency_limit: manual_review

Label rationale on file:

> A diagnostic medial branch block with 80 percent relief is documented, satisfying the hybrid block clause (80 >= 50 by rule, block documented by judgment). The frequency limit requires claims history that no source provides, so it defers to manual review: the tool must not auto-approve a frequency-limited procedure it cannot count. Axial facet pain without radiculopathy has no matching code in the current candidate set, so no code is expected. Note: this gold no-code label especially needs certified-coder review once the code seed broadens.

### System output (live run 2026-07-09)

- determination: manual_review
- denial risk score: 0.50
- recommended codes: M54.59
- CODE MISMATCH vs gold label: this case needs your call.

### Questions for the reviewer

1. KNOWN DISAGREEMENT (code): the gold label expects NO code (axial facet-mediated pain without radiculopathy) but the system recommends M54.59 (other low back pain), citing the documented axial low back pain. Is M54.59 acceptable for facet-mediated axial pain here, is a more specific code correct, or is the gold no-code label right? Note the candidate set is currently 25 ortho/pain codes; if the truly correct code is missing from it, say so.

### Adjudication (to be completed by the reviewer)

- Codes accepted or revised: 
- Determination accepted or revised: 
- Clause statuses accepted or revised: 
- Notes: 
- Reviewer name and credential: 
- Date: 

---

## Case 11: knee-injection-no-policy-medicare

- Requested service: major joint injection, knee
- Date of service: 2026-06-01
- Payer: Medicare

### Note (synthetic)

```
SYNTHETIC NOTE. Not a real patient. Evaluation fixture only.

Chief complaint: Low back pain radiating to the left leg, 8 weeks duration.

History of present illness: Gradual onset low back pain radiating into the
left posterior thigh and calf. Worse with prolonged sitting and forward
flexion, partially relieved by rest. Denies saddle anesthesia, bowel or
bladder dysfunction, fever, or unexplained weight loss.

Conservative care to date: Completed 6 weeks of physical therapy with minimal
improvement. Naproxen with partial relief. No prior epidural steroid injection.

Physical exam: Positive straight leg raise on the left at 40 degrees.
Diminished sensation in the left L5 dermatome. Strength 4/5 left extensor
hallucis longus. Reflexes symmetric.

Assessment: Lumbar radiculopathy, left L5 distribution, persistent despite 6
weeks of conservative therapy.

Imaging history: No prior lumbar MRI or other advanced imaging has been
performed for this episode.

Plan: Order lumbar MRI without contrast to evaluate for disc herniation or
nerve root compression given failure of conservative management.
```

### Current gold labels (placeholder, to adjudicate)

- expected_codes: (none)
- expect_refusal: true
- expected_denied: null (excluded from denial metrics: refusal or manual_review)

Label rationale on file:

> No loaded Medicare policy governs a knee joint injection, so the system should refuse before any model call rather than validate against an inapplicable policy. Verifies the runner scores refusals correctly.

### System output (live run 2026-07-09)

- determination: refused before model call
- denial risk score: n/a
- recommended codes: (none)

### Questions for the reviewer

1. Standard checklist above.

### Adjudication (to be completed by the reviewer)

- Codes accepted or revised: 
- Determination accepted or revised: 
- Clause statuses accepted or revised: 
- Notes: 
- Reviewer name and credential: 
- Date: 

---

## After the review

Apply the adjudicated labels to the cases YAML, re-run `uv run medilens evaluate`, and commit both together. From that commit onward the metrics are coder-adjudicated and may be quoted as such (still against synthetic policies).
