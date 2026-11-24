# Demo script (about 90 seconds)

This is the recording linked from the README. Anyone can repeat it in a Codespace.

## Before you record

1. Open a Codespace on `main` with the Codespaces secrets `GROQ_API_KEY` and `DUFFEL_TEST_TOKEN` set.
2. Run `make demo-public` and wait for "crewgate is up". Open the Console link it prints.
3. Make the browser window about 1280 x 800, and close other tabs so nothing private shows.
4. Ask one throwaway question first ("hello"), so the first model call isn't the slow one on camera.

## The takes

**1. Sign in (10 s).** Sign in as `anna` / `dev-only-anna`. The console shows who you are and
your role (planner).

**2. Read with the user's own permissions (20 s).** Ask:

> Who can cover a cabin crew duty at GOT tomorrow?

Open the steps panel next to the reply. Point at the `crew_find_cover` call through the gateway,
and the fact that contact details never appear.

**3. A write needs a person (25 s).** Ask:

> Propose swapping the first standby person onto that duty.

The reply says it is waiting for approval and gives a link. Open it. The approval page says in
plain words what will change and asks you to sign in **again**. Sign in, approve, and go back to
the console. Ask "Did the swap go through?" and show `gateway_approval_status` returning
`executed`.

**4. The booking guard (25 s).** Ask:

> Find me a flight from LHR to STN next Friday and book the cheapest one for Anna Svensson.

Duffel's sandbox changes the price on this route after search. Approve the booking. The gateway
checks the price again, sees it moved, and books nothing. The reply says the price changed and
the flight is **not** booked. Open the steps panel and show the guard's decision.

**5. Dashboards (10 s).** Open `/grafana/d/crewgate`: tool calls, refusals, approvals and latency
from the last few minutes.

## Recording on a Mac

1. Press Cmd+Shift+5, choose "Record Selected Portion", draw around the browser window and click
   Record. Stop it from the menu bar.
2. Open the file in QuickTime Player, use Edit, Trim to cut the start and end, and save it.
3. Keep the file small (under 10 MB): 1280 x 800 and under two minutes usually is. If it's
   bigger, QuickTime's File, Export As, 720p makes it smaller.
4. On github.com, open `README.md`, click the pencil, and drag the video onto the line that says
   `DEMO-VIDEO`. GitHub uploads it and puts a link there. Delete the comment line and commit.
