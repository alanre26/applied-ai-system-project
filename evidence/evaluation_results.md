# PawPal+ Evaluation Results

Run date: November 23, 2026 · Live Claude cases: run

**21 of 21 cases passed.**

| # | Category | Input | Criteria | Result | Notes |
|---|---|---|---|---|---|
| 1 | Retrieval | "Medication" (dog) | top note is medication.md | Pass | got medication.md |
| 2 | Retrieval | "Morning walk" (dog) | top note is dog_exercise.md | Pass | got dog_exercise.md |
| 3 | Retrieval | "Dinner" (cat) | top note is feeding_cats.md | Pass | got feeding_cats.md |
| 4 | Retrieval | "Litter box" (cat) | top note is litter_box.md | Pass | got litter_box.md |
| 5 | Retrieval | "Litter box" (dog) | top note is nothing | Pass | got nothing |
| 6 | Retrieval | "My dog ate chocolate" (dog) | top note is vet_warning_signs.md | Pass | got vet_warning_signs.md |
| 7 | Retrieval | "Can I give my dog ibuprofen?" (dog) | top note is medication.md | Pass | got medication.md |
| 8 | Retrieval | "Who won the World Series this year?" (dog) | top note is nothing | Pass | got nothing |
| 9 | Guardrail | Off-topic question | Claude is not called, owner told to ask elsewhere | Pass | Claude calls: 0 |
| 10 | Guardrail | Empty question | Handled without an error | Pass | Please type a question first. |
| 11 | Guardrail | Agent tries to skip medication | Change rejected, meds still planned, retry fixes plan | Pass | rejected on try 1 (must-do), fixed on try 2 |
| 12 | Guardrail | Agent suggests invalid time and unknown task | Both rejected | Pass | rejected: invalid time, unknown task |
| 13 | Guardrail | Claude unreachable | Falls back to rule-based plan, conflict still reported | Pass | plan has 5 tasks, used_ai=False |
| 14 | Guardrail | Claude returns invalid JSON | Falls back without crashing | Pass | Plan (try 1): Claude's reply wasn't valid JSON (Expecting value: line 1 column 1 (char 0)) |
| 15 | Guardrail | Agent run before owner accepts | Owner's saved tasks are unchanged | Pass |  |
| 16 | Live Ask | Missed medication dose | Uses Claude, cites [medication.md], under 150 words | Pass | 102 words |
| 17 | Live Ask | Dog ate chocolate | Uses Claude, tells owner to contact a vet | Pass | sources: ['vet_warning_signs.md'] |
| 18 | Live Ask | Best dog food brand (not in notes) | Doesn't name a brand, says notes don't cover it or to ask a vet | Pass | sources: ['feeding_dogs.md', 'feeding_cats.md'] |
| 19 | Live Agent | Dinner and litter box both at 18:00 | No conflicts left, all must-do tasks planned | Pass | attempts: 1, accepted: ['Litter box (Luna): time -> 18:10', 'Play time (Luna): skip today'] |
| 20 | Live Agent | "I'm busy from 7 to 9am" | No planned task overlaps 07:00-09:00, meds still planned | Pass | tasks still in 7-9am: none, attempts: 1 |
| 21 | Live Agent | Same request run 3 times | Every run ends with no problems (consistency) | Pass | 3/3 clean, attempts: [1, 1, 1] |

## AI outputs for human review

**Input:** What should I do if I miss a medication dose for Mochi?

> If you miss a dose for Mochi, **don't give two doses at once** to make up for it. Instead, call your vet and ask what to do [medication.md]. Missing a dose can make some treatments stop working, and for medicines like insulin or seizure meds it can be dangerous, so it's worth calling promptly [medication.md].
> 
> To help avoid missed doses, give each dose at the same time every day [medication.md]. If Mochi is on antibiotics, finish the full course even if he seems better [medication.md]. For monthly flea, tick, or heartworm preventatives, set a reminder for the same day each month [medication.md].

**Input:** Mochi ate a chocolate bar. What should I do?

> Please call your vet or an emergency clinic right away. Eating something toxic, including chocolate, is one of the warning signs that needs a vet's help [vet_warning_signs.md]. Don't wait to see if Mochi seems okay first.
> 
> When you call, it may help to have details ready, like what kind of chocolate it was, roughly how much Mochi ate, and when. I can't tell you how serious this is or what treatment Mochi needs, since PawPal can't work out what is wrong with a sick pet [vet_warning_signs.md]. Your vet can give you the right next steps.

**Input:** What brand of dog food is the best?

> I don't have any notes on dog food brands, so I can't recommend one. Your vet is the best person to ask what suits Mochi.
> 
> My notes do cover how to feed Mochi. Most adult dogs eat two meals a day, about 8 to 12 hours apart [feeding_dogs.md]. Feeding at roughly the same times each day helps [feeding_dogs.md]. Measure the food rather than guessing, and use the amount on the bag as a starting point. Your vet can tell you if Mochi needs more or less [feeding_dogs.md]. Treats should make up only about 10 percent of daily food [feeding_dogs.md].
> 
> Also, don't let Luna eat Mochi's food, since dog food doesn't have everything a cat needs [feeding_cats.md].

**Input:** Agent: (no request), dinner and litter box overlap

> I moved the litter box to 18:10 so it no longer clashes with Mochi's dinner. I also left out Luna's play time today, since all tasks together need 70 minutes and you have 60.

**Input:** Agent: I'm busy from 7 to 9am today.

> Your morning tasks now start at 9:00, after your busy window, with Mochi's medication first. Luna's litter box moves to 18:10 so it no longer clashes with Mochi's dinner, and play time is skipped today to stay within your 60 minutes.
