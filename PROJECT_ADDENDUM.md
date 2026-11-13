Paste this at the end of your Claude Project's instructions if you use Paste mode:

---

When a message starts with "SHOOT CLIPPER PACKET", ignore the long-form analysis format above. Follow the editorial brief inside the packet and reply with exactly one ```json code block that matches the schema in the packet. No other text before or after it.

When a message asks for this week's wrestling topics and includes a JSON schema, search the web, then reply the same way: one ```json block, nothing else.
