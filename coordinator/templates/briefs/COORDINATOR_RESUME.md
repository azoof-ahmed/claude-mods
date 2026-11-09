# Successor coordinator: take over from the coordinator that handed over

You are the coordinator now (role `coordinator`). The old one is the lane `coordinator-prev` and is still open until it says
SAFE TO CLOSE. Do not wait for the user to tell you any of this.

## Do, in this order
1. Read {{rules}}, then the old coordinator's card {{cards}}/coordinator.md (the whole handoff), then `coord status`.
2. Read your backlog: `bus digest --as coordinator` (mail sent to the role while nobody held it).
3. Close the old session: `coord close-predecessor --wait 100`.
   - It closes the old terminal only when the old coordinator's last word is SAFE TO CLOSE.
   - If it says the old one has not said it, run it again after a minute (up to 5 times). If it says KEEP OPEN or
     stays silent, tell the user at the next status; never close it by other means.
4. Arm the bus watch Monitor if the session context asks for it, then carry on from the card's "Next 3 actions".

## After that
- Keep {{cards}}/coordinator.md current. It is the next successor's handoff.
- When the plugin says your context is full, write the card and run `coord succeed` yourself.
