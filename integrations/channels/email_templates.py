"""Campaign copy for outbound email.

Kept as data rather than buried in a script so the wording can be reviewed and
argued with, which is the part that actually decides whether anyone replies.

The first version read like a product announcement: "You created a Hevolve
account a while back. Nunba is our desktop AI companion." Accurate, and nobody
would ever act on it. No reason to care today, no voice, sent from a system
address.

What this version does:

  * It is a gift, and says so. Nunba is free forever, not a trial and not a
    freemium tier with the good parts locked. That is unusual enough to be
    worth leading with, and it is true, so we can say it plainly.
  * It is from a person. Sathish runs the company. A note from him is more
    honest than one from a role account, and more likely to get a reply.
  * It leads with the reader's problem: paid assistants cost real money every
    month and everything typed into them goes to somebody else's server.
  * Rupees, not dollars. Most of this list is in India and abstract pricing
    persuades nobody.
  * The privacy claim is made checkable by linking the source. A promise you
    can verify is worth more than one you cannot, and it disarms exactly the
    skepticism the claim invites.
  * Plain formatting on purpose. Heavy marketing styling reads as a blast and
    costs trust; this should look like an email a person wrote.
  * No em dashes, short sentences, no hype words.

Every claim here is checkable against what Nunba actually ships: local
execution, no subscription, no telemetry, and the 8GB figure.
"""
from __future__ import annotations

SUBJECT = "Your own AI, free forever, running on your laptop"

TEXT = """Hi,

I am Sathish. I run Hevolve in Chennai. You signed up with us a while ago, so
I wanted to write to you myself.

We spent the last two years building something called Nunba, and I would like
to give it to you. Not a free trial. Not a limited tier with the useful parts
locked away. The whole thing, free, and it stays free.

Here is what it does. Nunba runs a real AI model directly on your own laptop.
Not in the cloud. Your conversations never leave your machine, because there
is nowhere else for them to go. It runs on a laptop with 8GB of RAM.

Most AI assistants now cost around 1,600 rupees every month, and every word
you type into them lands on a company's servers. We thought people should
have the other option.

You do not have to take my word on the privacy part. The code is open, so you
or anyone you trust can read exactly what it does:
https://github.com/hertz-ai/Nunba

Download it here:
https://hevolve.ai/download?ref=email

If you try it, tell me what breaks. We are a small team and honest criticism
is worth far more to us than polite silence. Replying to this email reaches
me directly.

If you would rather not hear from me again, reply with the word unsubscribe
and I will take you off this list today.

Sathish
Hevolve AI, Chennai
"""

HTML = """\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,\
Helvetica,Arial,sans-serif;font-size:16px;line-height:1.65;color:#1a1a1a;\
max-width:520px;margin:0 auto;padding:8px">

  <p style="margin:0 0 18px">Hi,</p>

  <p style="margin:0 0 18px">I am Sathish. I run Hevolve in Chennai. You
  signed up with us a while ago, so I wanted to write to you myself.</p>

  <p style="margin:0 0 18px">We spent the last two years building something
  called <b>Nunba</b>, and I would like to give it to you. Not a free trial.
  Not a limited tier with the useful parts locked away. The whole thing,
  free, and it stays free.</p>

  <p style="margin:0 0 18px">Here is what it does. Nunba runs a real AI model
  <b>directly on your own laptop</b>. Not in the cloud. Your conversations
  never leave your machine, because there is nowhere else for them to go. It
  runs on a laptop with 8GB of RAM.</p>

  <p style="margin:0 0 18px">Most AI assistants now cost around
  <b>1,600 rupees every month</b>, and every word you type into them lands on
  a company's servers. We thought people should have the other option.</p>

  <p style="margin:0 0 18px">You do not have to take my word on the privacy
  part. The code is open, so you or anyone you trust can read exactly what it
  does:<br>
  <a href="https://github.com/hertz-ai/Nunba"
     style="color:#0a5c3e">github.com/hertz-ai/Nunba</a></p>

  <p style="margin:0 0 18px">Download it here:<br>
  <a href="https://hevolve.ai/download?ref=email"
     style="color:#0a5c3e">hevolve.ai/download</a></p>

  <p style="margin:0 0 18px">If you try it, tell me what breaks. We are a
  small team and honest criticism is worth far more to us than polite
  silence. Replying to this email reaches me directly.</p>

  <p style="margin:0 0 6px">Sathish<br>
  <span style="color:#666">Hevolve AI, Chennai</span></p>

  <p style="margin:28px 0 0;padding-top:14px;border-top:1px solid #e8e8e8;\
font-size:13px;color:#888">
    You are getting this because you registered at hevolve.ai. Reply with the
    word <b>unsubscribe</b> and I will take you off this list today.
  </p>
</div>
"""
