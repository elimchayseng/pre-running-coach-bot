# Training Plan: Brooklyn Half → Broken Arrow

**Generated:** 2026-05-08
**Coverage:** May 8 → June 20, 2026

## Active Goals

- **A race:** RBC Brooklyn Half Marathon — Sat May 16, 2026. Target sub-1:21 (NYC age-group qualifier).
- **Fun race:** Broken Arrow Sky Race 46K — Sat June 20, 2026. No time goal, just finish.
- **Fall goal:** TBD. Sub-3:00 marathon under consideration but not committed.

---

## Phase 1 — Brooklyn Half Race Block (May 8 → 16)

### This Week (2026-05-08 → 2026-05-16)

| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Fri | 2026-05-08 | Rest + gentle yoga PM 30-40min | — | Hip/hamstring focus |
| Sat | 2026-05-09 | Easy 8mi STRICT | 8:30-9:00, HR ≤155 | Cut from 9-10mi due to unprescribed Wed ride |
| Sun | 2026-05-10 | Cycling 60-75min, NO climbing | HR <140 | Done before 4:30pm family Zoom |
| Mon | 2026-05-11 | Easy 4mi + restorative yoga PM | 8:30-9:00, HR ≤155 | Race week begins |
| Tue | 2026-05-12 | 5mi w/ 3x1000m + strength primer PM | 6:00-6:05 reps | Only lift this cycle; around 9:45am-1:15pm Google call |
| Wed | 2026-05-13 | Optional 20min spin OR rest | HR <125 | Start shifting sleep to ET |
| Thu | 2026-05-14 | AM fly SFO→Newark / PM 3mi shakeout + strides | easy | Run after arrival in Williamsburg |
| Fri | 2026-05-15 | Walk 10-15min + 5min mobility PM | — | Ankle/hip prep |
| Sat | 2026-05-16 | **BROOKLYN HALF** | 1:21:00 (6:10) | 7am ET start |

### Brooklyn Half Race Plan

**Hard rules (non-negotiable):**
- Mile 1 must be **6:15-6:20** (not 6:10, not 6:05)
- If mile 1 comes in under 6:15, BACK OFF immediately
- HR cap miles 1-3: 178 bpm

**Pacing strategy:**
- Miles 1-3: 6:15-6:20 (controlled, sit on the leash)
- Miles 4-8: 6:10-6:13 (settle in, find rhythm)
- Miles 9-11: 6:08-6:10 (now push)
- Miles 12-13.1: 6:00-6:05 (empty tank, earned by patience)

**Checkpoints:**
- 5K: 19:20 (NOT 18:45 — if early, wrong)
- 10K: 38:30
- Finish: 1:21:00

**Fueling:**
- Pre-race ~16oz fluid, normal breakfast 2.5-3 hrs out
- Gel at mile 4 and mile 8
- Caffeine if normally used; nothing new

**Why these rules:** Thu 5/7 test ran 5:57/6:00/6:06 on prescribed 6:10-6:15 with HR hitting 190 on rep 2. Fitness is there for 1:21; pacing discipline is the limiter.

---

## Phase 2 — Brooklyn → Broken Arrow Bridge (May 17 → June 20)

**Status:** Skeleton only. Week-by-week details TBD pending fall-goal decision.

- **Week 1 (May 17-23):** Recovery from Brooklyn — easy runs, swimming, no structure.
- **Weeks 2-4 (May 24 → Jun 14):** Trail running with vert; prep for Broken Arrow terrain.
- **Week 5 (Jun 15-20):** Shakeout runs into Broken Arrow weekend.
- **Race day (Jun 20):** Broken Arrow Sky Race 46K, Palisades Tahoe, ~8-10K ft vert. Fun race, no time goal.

### Open Questions Before Building Phase 2 Out

1. **Fall goal commitment.** Sub-3:00 marathon (CIM, NYC if Saturday qualifies, other)? Broken Arrow as A race? Determines whether June is base-building, vert-specific, or recovery-focused.
2. **Altitude exposure.** Broken Arrow tops 8,000+ ft. Recent altitude exposure or need an acclimation plan?
3. **Trail/vert prescription style.** Specific routes/segments, or weekly vert numbers?

### Not Yet Built

- Week-by-week daily structure for May 24 → Jun 14
- Vert progressions for altitude / technical demands
- Strength training schedule for this phase
- Cross-training rhythm
- Fall goal decision

---

## Active Coaching Adjustments

### Pacing discipline (active issue)
Confirmed handoff note about external load checks. Thu 5/7 ran 13-18 sec/mi faster than prescribed on HM test. Race-day rules now include hard pace floor on mile 1 with explicit back-off instruction.

### Unprescribed volume (active issue)
Two extra sessions in week of 5/4:
- Mon 5/4: 3mi run (was rest day)
- Wed 5/6: 18.18mi ride / 1,266ft / RE 29 (prescribed: easy spin HR <130 OR swim)

Resulting adjustments: Sat long run cut to 8mi, Sun ride cut to 75min with no-climbing rule, Tue 5/5 strength missed and will not be made up.

### Race week travel
- Thu 5/14: UA 419 SFO→Newark, 6am departure, ~2:37pm ET arrival
- Hoxton Williamsburg, 5/14-5/18
- Shakeout moved from 6:30am PT → ~3pm PT (post-arrival NYC)
- ET sleep adaptation starts Tuesday 5/12

---

## Adjustment Triggers (How the Coach Adapts)

Coach calls `get_fitness_summary(window_days=14)` before any non-trivial adjustment.

**Pace zone updates:**
- Don't adjust from a single session.
- Trailing 3 quality sessions consistently 5+ sec/mi faster at same/lower RPE → tighten zones, update `athlete.yaml`.
- Trailing 3 sessions 5+ sec/mi slower at higher RPE → loosen zones, document in journal.

**Volume / intensity backoff:**
- 2 consecutive weeks of RPE > prescribed at target paces → drop next week volume 20%, hold intensity.
- Any reported pain ≥3/10 → pull intensity immediately, replace with cross-train. Reassess 3-5 days. Don't return to plan until 2 consecutive pain-free runs.
- HRV trending down >10% over 7 days → flag, recommend 1-2 easy days swap.

**Daily-input adjustments (propose + confirm before writing):**
- Travel reported → reduce volume of next 1-2 days; cap intensity.
- Sleep <6hr two nights running → demote next quality session to easy.
- Weather extreme at planned workout time → propose reschedule or substitute.
- New niggle reported → see pain rule above.

**Auto-update triggers (proactive):**
- New PR session → update `prs`.
- Injury reported as resolved 7+ days pain-free → update `injury_history` status.
- Threshold/MP zones drift per the trend rules above.

All plan edits append to the change log below with reasoning.

---

## Recent Plan Adjustments

- 2026-05-08: Synced full plan from coaching conversation. Phase 1 daily table locked through Brooklyn (5/16). Phase 2 left as skeleton pending fall-goal decision. Pacing-floor and unprescribed-volume rules formalized.
- 2026-04-26: Created two-race plan (Brooklyn 5/16 → Broken Arrow 6/20). Boston-recovery skeleton + adaptive sharpening.

---

## Reference

### Target paces
- Half marathon (Brooklyn): 6:10-6:15
- Marathon: 6:40
- Threshold: 6:15-6:25
- Easy: 8:30-9:00
- Recovery: 9:00-9:30

### HR
- Resting: 44-48
- Easy ceiling: 155
- Threshold: 175-185
- Race max observed: 194-195

### Race-week strength protocol
- Last lift no later than 4 days pre-race
- Primer format: trap-bar DL 3x3 @ 60%, box jumps 3x3, single-leg calf raises 2x10
- No grinding, no failure sets, no accessory, no upper-body bonus

### Fixed calendar conflicts (race week)
- Tue 5/12: Google call 9:45am-1:15pm PT
- Sun 5/10: Family Zoom 4:30pm PT
- Thu 5/14: SFO→Newark 6am PT, Kelly's birthday
- 5/14-5/18: Hoxton Williamsburg
