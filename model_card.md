# Model Card: PawPal+ AI

## System overview

| | |
|---|---|
| **What it is** | A pet care planner that uses retrieval-augmented generation (RAG) and a planning agent on top of my original PawPal+ scheduler (Module 2). |
| **AI model** | Claude Opus 5.5 (`claude-opus-5-5`) through the Anthropic API, low effort, with server-side fallback turned on. |
| **What the AI does** | 1) Answers pet care questions using only retrieved care notes, with citations. 2) Suggests changes to today's plan (move a time, change a priority, skip a task today) based on the owner's request. |
| **What the AI does not do** | It doesn't diagnose pets, recommend medicines or doses, add or delete tasks, or save anything without the owner clicking Accept. |
| **Knowledge source** | 10 short pet care notes I wrote in `knowledge/`, covering dogs and cats. |
| **Intended users** | A pet owner with one or a few dogs or cats who wants help fitting daily care into a busy day. |

## Limitations and biases

- **Only dogs and cats, and only general advice.** The notes are general care for adult dogs and cats. There's nothing for rabbits, birds, or reptiles, and only small mentions of puppies, kittens, and older pets. Someone with a pet marked "other" only gets the notes tagged for all species. The advice also doesn't change by breed, size, or health condition, so a husky and a senior chihuahua get the same walking note.
- **The notes are mine.** I wrote them from common pet care guidance, but no vet reviewed them. If a note is wrong or missing something, PawPal will repeat that mistake confidently and cite it.
- **Keyword retrieval misses words it doesn't know.** If a question says "tablet" and the medication note is tagged "pill" and "meds", nothing is found and PawPal says it doesn't have notes on that. It fails safely, but it fails.
- **The care notes favor must-do tasks over everything else.** With little time, medication and feeding always win, even over a walk the dog might really need. In my demo, with 30 minutes free, the walk got dropped so the medication fit. I think that's the right call, but it's a choice I built into the system, not something the owner picked.
- **Claude mostly sticks to the notes, but not always.** In the chocolate answer, Claude added "say what kind of chocolate, how much, and when" and "don't try home treatments." That's good advice, but it wasn't in the notes and had no citation. The system prompt says to use only the notes, and that works most of the time, not every time.
- **The agent drops low-priority tasks on its own.** It skipped Luna's play time in every live run to fit 60 minutes. The owner only finds out from the summary and the "Not in today's plan" list.
- **Time is the only resource.** The planner doesn't know about travel time, weather, or energy. It also can't tell when doing two tasks at once is actually fine, like feeding the cat while the dog eats.
- **Data isn't saved.** Pets and tasks live in the browser session and disappear when the app restarts.

## Could it be misused, and how I prevent that

| Possible misuse | What the system does about it |
|---|---|
| Treating PawPal like a vet ("what's wrong with my dog?", "how much ibuprofen can I give?") | The system prompt forbids diagnosing or recommending medicines and doses. The notes themselves say never to give human medicine without a vet. Emergencies like eating chocolate get sent straight to a vet or emergency clinic. |
| Using it as a general chatbot | If a question doesn't match any care note, Claude is never called. The owner gets a fixed reply saying PawPal only answers pet care questions its notes cover. |
| The AI cutting something important, like medication | Guardrails reject any change that skips or lowers a task a care note marks as must-do, even if Claude suggests it. |
| The AI changing the owner's data without asking | The agent only works on a copy. Nothing is saved until the owner clicks Accept, and skips are never saved. |
| Running up API costs | Claude is only called on a button click, not on every page refresh, and the agent retries at most once. A spend limit can also be set in the Claude Console. |
| Leaking the API key | The key is only read from an environment variable, and the README says not to put it in any file in the repo. |

One thing I can't fully prevent is someone trusting PawPal's answer instead of calling a vet when they should. That's why every answer says where it came from, and the app says it isn't a replacement for a vet.

## Testing results

- **41 of 41 automated tests pass:** 16 for the original scheduler, 16 for RAG, and 9 for the agent. The AI tests use a fake Claude so they're repeatable and free.
- **21 of 21 evaluation cases pass** (`evidence/evaluation_results.md`): 8 retrieval, 7 guardrail, 3 live Ask PawPal answers, and 3 live agent runs.
- **Consistency:** the same agent request ran 3 times and came back clean all 3 times, each on the first try.
- **Logging:** every retrieval, priority change, Claude call, rejected change, and fallback is written to `pawpal.log`.

The evaluation is small, though. 21 fixed cases with simple checks (like "does the answer mention a vet") show it works on these inputs. They don't prove it works on every input.

## What surprised me while testing

- **The worst bug came from one ordinary word.** "Who won the World Series this year?" matched the vet note because it says "once a year", so an off-topic question would have reached Claude. My original test used "Who won the World Series?" without "this year", so it passed and hid the problem. It only showed up when the full demo ran and the output was read line by line. I fixed it with a minimum match score and added a test for that exact question.
- **"Dog" and "cat" were ruining the search.** They appear in note titles, so they outscored the words that mattered, and "My dog ate chocolate" didn't find the vet note at first.
- **My tests had bugs too.** One evaluation check failed even though the agent did the right thing. It only looked at the agent's last retry, not the first try where the bad change was rejected. Another check passed no matter what because of an `and`/`or` mistake. A check that always passes is worse than no check, because it looks like proof.
- **Claude was more reliable than I expected,** once it got the right notes. All the live cases passed on the first try, and when I asked for the best dog food brand it said it didn't have notes on brands instead of making one up. Most of my problems were in the retrieval and checks around the AI, not the AI itself.

## AI collaboration

I built this project with Claude (Claude Code) as a coding assistant in VS Code. I used it to plan which AI features fit PawPal, write the RAG and agent code, write tests, run everything, and draft documentation. I made the decisions about what the project should be. I chose RAG plus a light agent, asked for it to stay simple, ran the live scripts with my own API key, and reviewed the output before pushing.

**A suggestion I accepted:** when I asked which of the four AI features fit PawPal best, Claude suggested RAG as the main feature, a light agent that wraps my existing scheduler instead of replacing it, and skipping the fine-tuned model because it needs training data and doesn't fit a scheduler. I went with that because it kept all of my original PawPal+ code and tests working, and each feature actually changes how the app behaves. The care notes change priorities, and the agent changes times.

**A suggestion I did not accept as written:** the first version of the new features was added straight onto the old PawPal+ page. It had a "Use care notes" toggle and an "Ask PawPal" box stacked under everything else on one long page. It worked, but it looked cluttered, outdated, and confusing, a lot like the old app. I asked for it to be redone, and it became a sidebar for the owner and adding pets, a separate card for each pet with its own tasks, and tabs for Pets & Tasks, Today's Plan, and Ask PawPal. Claude also offered extras I didn't take, like support for multiple owners and confidence scores. I left them out because they would have made the project more complicated without making it more useful, and I wanted it simple.

**What I had to verify myself:** the AI wrote code fast, but I still had to run it and look at what it produced. I ran the demo and evaluation with my real API key, and the full output got read, not just the pass/fail marks. That's how the confusing "ask your vet" reply to a baseball question got caught, even though that case passed. The AI also can't see my API key, so the live tests only happened because I ran them.
