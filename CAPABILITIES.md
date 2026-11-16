# What SocialMCP can do

A private CRM built from your own social data export. Your AI assistant
queries it on your behalf, and the data never leaves your computer.

Ask in your own words — the shapes below are just the kinds of question it
can answer. Replace anything in angle brackets with your own details.

## Find people by what they do, not just by name

Search your network by meaning as well as keywords. A description of a role or background finds people whose titles use different words, and filters narrow by company, title, date met or event.

Ask like:
- *who in my network works in <field or role>*
- *find people at <company>*
- *who could help me with <problem>*

**Note:** Meaning-based search needs a local embedding model; without one, search falls back to matching words literally.

## Recall how and when you met someone

The note you sent with a connection request is kept and attached to that person, along with the date. That is usually where you were and what you talked about — the context that disappears from memory within weeks.

Ask like:
- *how do I know <person>*
- *remind me where I met <person>*
- *show everything you have on <person>*

**Note:** Only invitations you sent carry a note. When the other person invited you, there is usually no note to keep.

## Group connections into the events they came from

Bursts of connections over a few days are detected automatically and grouped, because that pattern is what a conference looks like in data. You can give a group its real name and then ask about it by that name.

Ask like:
- *what events are in my network*
- *name that group <event name>*
- *who did I meet at <event name>*

**Note:** The assistant works out an event's dates itself; SocialMCP only understands dates. Groups start with placeholder names until you name them.

## See who reached out to whom

Every connection records which side sent the invitation and whether it was accepted, so you can tell the people who sought you out from the people you chased.

Ask like:
- *who reached out to me at <event or period>*
- *how many of my invitations were accepted*

**Note:** The export's invitation history covers roughly the last six months, so for older connections it cannot say who invited whom.

## Know which invitations are waiting on you, and which on them

Outstanding invitations are separated by who owes the next action: ones you sent that have not been accepted, and ones you received that you have not answered. Only the second kind can be cleared by you.

Ask like:
- *which invitations are waiting on me*
- *which of my invitations were never accepted*
- *show invitations older than <number> days*

**Note:** An invitation that vanishes from a later export without connecting is marked lapsed — declined, withdrawn or expired; the export does not say which.

## Find connections you have never actually spoken to

Being connected and having had a conversation are different things. People you connected with but never messaged are listed separately from people whose invitation is still outstanding.

Ask like:
- *who have I never followed up with*
- *who did I meet recently and never message*

**Note:** Only messages present in the export count as contact.

## Keep private notes on people

Add your own timestamped notes to anyone — what they are working on, what you promised, why you are not pursuing them. Stored only on this computer, and searchable alongside everything else.

Ask like:
- *make a note on <person>: <what you want to remember>*
- *what notes do I have on <person>*

**Note:** Notes never leave your machine and are not sent to any social network.

## Attach public information your assistant found

When your assistant looks something up about a person from public sources, the finding can be filed against that contact with its source, so it is kept and searchable. Your own notes stay distinguishable from anything that was looked up.

Ask like:
- *look up what <company> does and save it to <person>*
- *save this finding to <person> with the source*

**Note:** SocialMCP never fetches anything itself and never reads any social network. The assistant does the lookup, one person at a time, and only when asked.

## See who changed jobs since your last export

When a newer export shows a different company or title for someone, the change is recorded with the date it was noticed, so a fresh export tells you who moved.

Ask like:
- *who changed jobs recently*
- *has anything changed in my network*

**Note:** Needs at least two exports taken far enough apart to differ.

## Load a new data export

Drop a new export into the watched folder and it is read automatically: new people added, existing people updated, nothing duplicated. You can also trigger it on demand.

Ask like:
- *load my new export*
- *what changed since last time*

**Note:** Use the archive file exactly as downloaded, without unzipping it. Re-loading the same export changes nothing.

# What it cannot do

- Reads only the data export you download from the social network yourself. It never logs in, never uses your password or cookies, and never reads any website.
- Cannot see anything the export leaves out. Your connections' full profiles are not in it — only their name, company, title and the date you connected.
- Cannot tell 2nd from 3rd degree. Anyone absent from your connections list is simply not a connection; the export does not say how far away they are.
- Cannot send messages, accept invitations or change anything on the social network. It is read-only by design.
- Invitation history covers roughly the last six months, so older connections cannot be attributed to whoever sent the invitation.
- Knows nothing about events by name on its own. Your assistant supplies the dates; SocialMCP matches them against when you met people.
- Everything is stored in one file on this computer. Nothing is uploaded, and deleting that file deletes all of it.
