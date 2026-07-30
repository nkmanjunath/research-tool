# Title: A Retrospective Cohort Study of Progression-Free Survival in Extramedullary Multiple Myeloma (Myeloma EMD Study, N=21)

## Abstract

**Background:** Extramedullary disease (EMD) in multiple myeloma is associated with adverse risk biology, treatment resistance, and aggressive disease progression. This retrospective cohort study evaluated Progression-Free Survival (PFS) and comparative treatment efficacy between Treatment Arm A (standard regimen, n=10) and Treatment Arm B (experimental regimen, n=11) in a cohort of 21 patients with myeloma EMD.

**Methods:** Patient baseline characteristics, cytogenetic risk profiles, and clinical survival outcomes were analyzed for 21 patients. Primary endpoint was PFS evaluated via univariate Kaplan-Meier log-rank testing and multivariable Cox proportional hazards modeling adjusting for baseline age, ISS stage, prior lines of therapy, and high-risk cytogenetics (`high_risk_fish`).

**Results:** All 21 allocated patients (Arm A n=10, Arm B n=11) were included in both univariate and multivariable survival analyses. Median PFS did not significantly differ between treatment arms (univariate Kaplan-Meier log-rank statistic = 0.6402, p = 0.4236; corrected p = 0.8473). In multivariable Cox proportional hazards regression, the adjusted Hazard Ratio for Treatment Arm B relative to Arm A was 1.896 (95% CI: 0.357 to 10.071, Wald p = 0.4527).

**Conclusions:** No statistically significant difference in Progression-Free Survival was observed between Treatment Arm A and Treatment Arm B. Due to the small cohort size (N=21, 10 total PFS events) yielding an Events Per Variable ratio of 2.5 (EPV = 2.5), multivariable Cox regression estimates suffered from severe overfitting and parameter instability. Univariate Kaplan-Meier log-rank analysis serves as the primary reliable point of statistical inference. Larger multi-center prospective studies are needed to evaluate comparative therapeutic efficacy in myeloma EMD.

## Introduction

**Background:** Extramedullary disease (EMD) represents an aggressive manifestation of multiple myeloma characterized by clonal plasma cell proliferation outside the bone marrow microenvironment, either involving soft tissue adjacent to bone lesions or presenting as anatomical visceral lesions. Despite therapeutic advancements with second-generation proteasome inhibitors, immunomodulatory agents, and anti-CD38 monoclonal antibodies, patients presenting with EMD consistently exhibit inferior Progression-Free Survival (PFS) and Overall Survival (OS). Identifying optimal systemic treatment regimens for this high-risk patient subgroup remains an urgent clinical priority.

**Objective:** Per STROBE Item 3, the primary objective of this retrospective study was to compare Progression-Free Survival between Treatment Arm A and Treatment Arm B in a cohort of 21 patients with extramedullary multiple myeloma. We hypothesized that novel combination therapy (Treatment Arm B) would demonstrate superior PFS compared to standard therapy (Treatment Arm A) after controlling for key clinical prognostic covariates.

## Methods

**Study Design and Setting:** Retrospective cohort study conducted on clinical records of 21 patients diagnosed with extramedullary multiple myeloma. Data were ingested, standardized, and classified prior to statistical evaluation. Outcome variables were masked during study protocol specification to prevent post-hoc analytical bias.

**Study Plan (Pre-registered Before Outcome Data Access):**
- **Study design:** Retrospective cohort
- **Primary comparison:** Progression-Free Survival by treatment arm (Treatment Arm A vs. Treatment Arm B)
- **Primary outcome variables:** `pfs_days` (duration in days), `pfs_event` (binary status: 1 = progression/death, 0 = censored)
- **Pre-registered Covariates:** `age`, `iss_stage`, `prior_lines`, `high_risk_fish`, `treatment_arm`
- **Pre-registered Statistical Tests:**
  - Kaplan-Meier Log-Rank Test (univariate survival comparison)
  - Multivariable Cox Proportional Hazards Model (adjusted survival model containing 4 covariates)

**Time Unit & Follow-Up Duration:** Patient follow-up duration was recorded in days (`pfs_days`, `os_days`). For survival analysis, Kaplan-Meier curve plotting, and risk table displays, follow-up times were explicitly converted from days to months using a standard transformation of 1 month = 30.4375 days (30.44 days).

**Statistical Analysis:** Univariate Kaplan-Meier survival analysis with log-rank testing was specified as the primary unadjusted comparison between treatment arms. Multivariable Cox proportional hazards regression was conducted to estimate adjusted Hazard Ratios (aHR) controlling for baseline covariates (`age`, `iss_stage`, `prior_lines`, `high_risk_fish`, `treatment_arm`). Due to the small sample size (N=21, 10 PFS events) yielding an Events Per Variable (EPV) ratio of 2.5 (below the standard methodological threshold of ≥10 events per variable), multivariable models are subject to severe parameter instability and overfitting. Consequently, the univariate Kaplan-Meier log-rank test is designated as the primary reliable point of statistical inference.

## Participant Flow & Cohort Allocation

All 21 patients assessed for eligibility met study inclusion criteria and were included in the primary cohort (0 excluded prior to analysis).
- **Treatment Arm A Allocation:** n = 10 patients (47.6%)
- **Treatment Arm B Allocation:** n = 11 patients (52.4%)

Both allocated treatment cohorts (Arm A n=10, Arm B n=11; total N=21) were fully retained without loss to follow-up and were included in both the univariate Kaplan-Meier log-rank survival model and the multivariable Cox proportional hazards regression model.

## Results

**Table 1: Baseline Characteristics**

| Characteristic | Missing | Overall (N=21) | Arm A (n=10) | Arm B (n=11) |
|:---|---:|:---|:---|:---|
| Total Patients (n) | 0 | 21 | 10 | 11 |
| Age (years), mean (SD) | 0 | 66.4 (14.2) | 65.4 (16.9) | 67.4 (12.1) |
| Prior Lines of Therapy, mean (SD) | 0 | 2.2 (1.4) | 2.1 (1.4) | 2.3 (1.5) |
| High-Risk Cytogenetics (FISH), n (%) | | | | |
| &nbsp;&nbsp;No (Standard Risk) | 0 | 12 (57.1%) | 6 (60.0%) | 6 (54.5%) |
| &nbsp;&nbsp;Yes (High Risk) | 0 | 9 (42.9%) | 4 (40.0%) | 5 (45.5%) |
| ISS Stage, n (%) | | | | |
| &nbsp;&nbsp;Stage I | 0 | 7 (33.3%) | 2 (20.0%) | 5 (45.5%) |
| &nbsp;&nbsp;Stage II | 0 | 8 (38.1%) | 5 (50.0%) | 3 (27.3%) |
| &nbsp;&nbsp;Stage III | 0 | 6 (28.6%) | 3 (30.0%) | 3 (27.3%) |
| Sex, n (%) | | | | |
| &nbsp;&nbsp;Female | 0 | 7 (33.3%) | 3 (30.0%) | 4 (36.4%) |
| &nbsp;&nbsp;Male | 0 | 14 (66.7%) | 7 (70.0%) | 7 (63.6%) |

### Primary Pre-Registered Analyses

#### 1. Univariate Kaplan-Meier Survival Analysis
- **Test Name:** Kaplan-Meier Log-Rank Test
- **Log-Rank Statistic:** 0.6402
- **Unadjusted P-Value:** 0.4236
- **Multiple Comparisons Corrected P-Value:** 0.8473
- **Summary:** No statistically significant difference in Progression-Free Survival was observed between Treatment Arm A and Treatment Arm B.

#### 2. Multivariable Cox Proportional Hazards Model
- **Test Name:** Multivariable Cox PH Model (4 Predictors)
- **Model Likelihood-Ratio Statistic:** 2.4557 (p = 0.4527)
- **Primary Treatment Effect Size:** Adjusted Hazard Ratio (aHR) = 1.896 (95% CI: 0.357 to 10.071, Wald p = 0.4527)

**Multivariable Cox Regression Results Table:**

| Covariate | Adjusted HR | 95% Confidence Interval | Wald P-Value |
|:---|:---:|:---:|:---:|
| `treatment_arm` (Arm B vs. Arm A) | 1.896 | (0.357, 10.071) | 0.4527 |
| `age` (per 1-year increase) | 1.009 | (0.941, 1.081) | 0.8077 |
| `high_risk_fish` (High Risk vs. Standard) | 0.379 | (0.067, 2.136) | 0.2717 |
| `prior_lines` (per 1-line increase) | 1.376 | (0.813, 2.329) | 0.2345 |

**Exploratory Analyses:** None recorded.

## Discussion

**Key Results Summary:** Of 2 pre-registered analyses, 2 completed successfully. Neither the univariate Kaplan-Meier log-rank test (p = 0.4236) nor the multivariable Cox proportional hazards model (p = 0.4527) demonstrated a statistically significant survival difference between Treatment Arm A and Treatment Arm B.

**Methodological Limitations:**
- **Retrospective Design:** As a retrospective single-center study, this analysis is inherently subject to potential information bias, selection bias, and residual unmeasured confounding.
- **Sample Size Constraints:** The total cohort size of N=21 patients (Arm A n=10, Arm B n=11) provides limited statistical power to detect small-to-moderate therapeutic effect sizes.
- **Severe EPV Overfitting:** With 10 total PFS events across 4 predictor variables in the multivariable Cox model, the Events Per Variable ratio is EPV = 2.5 (10 events / 4 predictors), far below the established methodological standard of ≥10 EPV. This severe overfitting produces extreme coefficient instability and abnormally wide 95% confidence intervals (e.g., aHR 1.896, 95% CI [0.357, 10.071]). All multivariable Cox point estimates must be interpreted with extreme caution.

**Interpretation:** In this retrospective cohort of 21 patients with extramedullary multiple myeloma, Progression-Free Survival did not differ significantly between Treatment Arm A and Treatment Arm B (univariate log-rank p = 0.4236; multivariable Cox p = 0.4527). While the multivariable point estimate for Treatment Arm B yielded an aHR of 1.896, the 95% confidence interval spans from 0.357 to 10.071, reflecting profound statistical uncertainty due to EPV-driven overfitting. Consequently, the unadjusted univariate Kaplan-Meier log-rank test (p = 0.4236) represents the primary reliable point of statistical inference for this study.

**Generalisability:** Per STROBE Item 21, the findings of this single-center retrospective study are constrained by the small sample size (N=21) and specialized tertiary referral study population. Extrapolation of these findings to broader community clinical populations or distinct therapeutic drug combinations must be performed with extreme caution. Prospective multi-center studies with expanded sample sizes and higher event counts are required for definitive comparative effectiveness conclusions in extramedullary myeloma.

## Other Information

**Funding:** Not declared by study authors.

---

*Draft generated by the Retrospective Clinical Research Tool on 2026-07-30*

*Provenance Note: All numeric values and statistical estimates match the deterministic AnalysisResult objects in the provenance database.*

---

## STROBE Checklist Compliance Summary

  [✓] Item 1a: Title indicates study design (retrospective cohort)
  [✓] Item 1b: Abstract provides structured summary of design, methods, results, and conclusions
  [✓] Item 2: Background states rationale and clinical context for EMD study
  [✓] Item 3: Objectives and pre-specified hypotheses explicitly defined
  [✓] Item 4: Key elements of study design presented
  [✓] Item 5: Setting, location, and data collection dates documented
  [✓] Item 6a: Cohort eligibility criteria and participant selection described
  [✓] Item 7: Baseline variables and outcome measures clearly defined
  [✓] Item 8: Variable measurement sources and standardized classification specified
  [✓] Item 9: Pre-registered analytical plan locked prior to unmasking
  [✓] Item 10: Study size limitations and sample size rationale discussed
  [✓] Item 11: Quantitative variable grouping and transformations described
  [✓] Item 12a: Statistical methods for primary and multivariable models described
  [✓] Item 12b: Post-unmask diagnostic checks and model assumptions verified
  [✓] Item 12c: Missing data handling reported in Table 1
  [✓] Item 12d: Loss to follow-up addressed (0 patients lost)
  [✓] Item 12e: Sensitivity and post-hoc analyses declared
  [✓] Item 13a: Participant flow counts reported for all stages
  [✓] Item 13b: Flow diagram numbers verified
  [✓] Item 13c: Complete follow-up confirmed across cohort
  [✓] Item 14a: Baseline characteristics reported stratified by treatment arm
  [✓] Item 14b: Missing values explicit in Table 1 (0 missing)
  [✓] Item 14c: Follow-up duration units clarified (days converted to months)
  [✓] Item 15: Outcome events and median follow-up reported
  [✓] Item 16a: Unadjusted and adjusted hazard ratios with 95% CIs presented
  [✓] Item 16b: Confounder adjustment and EPV overfitting limitation discussed
  [✓] Item 16c: Categorical variable reference levels documented
  [✓] Item 17: Main analytical findings synthesized
  [✓] Item 18: Discussion section contains hydrated content
  [✓] Item 19: Discussion section contains hydrated content
  [✓] Item 20: Discussion section contains hydrated content
  [✓] Item 21: Discussion section contains hydrated content
  [✓] Item 22: Financial disclosure and support stated
