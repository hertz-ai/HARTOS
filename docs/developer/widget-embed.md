# Embedding the Hevolve Agent on Any Page

Add the Hevolve AI chat agent to any website.

!!! warning "Start with Option 3. Options 1 and 2 are not deployed."

    Measured 2026-07-30 against the live hosts:

    | Endpoint | Result |
    |---|---|
    | `cdn.hertzai.com/mindstory.js` (Option 1) | **does not resolve, no DNS** |
    | `hevolve.hertzai.com/hevolve-widget.js` (Option 2) | **404** |
    | `hevolve.hertzai.com/agents/<Name>?embed=true` (Option 3) | 200 |
    | `hevolve.ai/agents/<Name>?plugin=1` ([Agent Plugin](../agent-plugin.md)) | 200 |
    | `hevolve.ai/v1` (Option 4) | 200 |
    | `@hertzai/mindstory` on npm | unpublished |

    Option 1 was marked Recommended, and its host has no DNS record at all, so
    the script tag fails to load rather than failing to work. It is the first
    thing anyone tries, which means the likely result of reading this page was
    concluding the product is broken. Options 1 and 2 are kept below as the
    intended interface, not as something you can integrate against today.

---

## Option 1: Chat Widget (NOT DEPLOYED)

!!! danger "cdn.hertzai.com does not resolve. This snippet cannot load."

One script tag. Floating chat pill appears bottom-right.

```html
<script src="https://cdn.hertzai.com/mindstory.js"></script>
<script>
  Mindstory.widget({
    apiKey: 'your-api-key',
    position: 'bottom-right',
    greeting: 'Hi! How can I help?',
    theme: {
      primary: '#6366f1',
      background: '#ffffff',
      text: '#1f2937'
    }
  });
</script>
```

### Widget Config

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `apiKey` | string | required | From `/api/v1/intelligence/keys` (free tier available) |
| `position` | string | `'bottom-right'` | `bottom-right`, `bottom-left`, `top-right`, `top-left` |
| `greeting` | string | `'Hi!'` | Welcome message |
| `placeholder` | string | `'Type a message...'` | Input placeholder |
| `multimodal` | boolean | `false` | Enable camera/file upload |
| `theme.primary` | string | `'#6366f1'` | Primary color |
| `theme.background` | string | `'#ffffff'` | Background color |
| `theme.text` | string | `'#1f2937'` | Text color |
| `theme.borderRadius` | string | `'12px'` | Corner radius |

---

## Option 2: HevolveWidget Script (NOT DEPLOYED)

!!! danger "hevolve-widget.js returns 404 and nothing currently builds that bundle. The React components it wraps do exist, so this is a packaging gap rather than a missing feature."

For more control over initialization and events.

```html
<script>
var script = document.createElement('script');
script.src = "https://hevolve.hertzai.com/hevolve-widget.js";
script.onload = function() {
  if (typeof HevolveWidget !== 'undefined') {
    var widget = HevolveWidget.init({
      agentName: 'Nunba',
      authToken: 'YOUR_TOKEN',
      userId: 'USER_ID',
      emailAddress: 'user@example.com'
    });

    widget.on('open', function() { console.log('Widget opened'); });
    widget.on('close', function() { console.log('Widget closed'); });
    widget.on('message', function(data) { console.log('Message:', data); });
  }
};
document.body.appendChild(script);
</script>
```

### Events

| Event | Data | Description |
|-------|------|-------------|
| `open` | — | Widget opened by user |
| `close` | — | Widget closed |
| `message` | `{text, video_url?}` | Response received (includes media) |

Video responses (Pupit talking-head, Mindstory narrative) render as inline players
with download buttons — no extra UI needed.

---

## Option 3: iframe Embed (works today)

Embed a full chat interface in any container. This and the
[Agent Plugin](../agent-plugin.md) are the two methods that are actually
deployed, and they are not interchangeable:

- **`?embed=true`**, below, is the full chat and can act as a specific user via
  `&token=` and `&user_id=`. Use it where you already know who the person is.
- **`?plugin=1`** is anonymous: no key, no login, audio only, and it
  guest-registers the visitor on mount. Use it on a docs or marketing page where
  you want someone talking to an agent before they have an account.

Note the two are served from different hosts, `hevolve.hertzai.com` and
`hevolve.ai`. That is how they are deployed, not a typo.

```html
<iframe
  src="https://hevolve.hertzai.com/agents/Nunba?embed=true&companionAppInstalled=true"
  width="400"
  height="600"
  frameborder="0"
  style="border-radius: 12px; border: 1px solid #333"
  allow="microphone; camera; autoplay"
></iframe>
```

To authenticate the user, append `&token=JWT_TOKEN&user_id=USER_ID` to the URL.

The `ShareDialog` in Hevolve generates this embed code for any resource
(agent, post, recipe, game) — use it to generate embed snippets for your content.

---

## Option 4: OpenAI-Compatible API

If your app already uses the OpenAI SDK, point it at HART OS (or `http://localhost:6777` if self-hosted):

=== "Python"

    ```python
    from openai import OpenAI

    client = OpenAI(
        base_url="https://hevolve.ai/v1",
        api_key="your-api-key",
    )

    response = client.chat.completions.create(
        model="hevolve",
        messages=[{"role": "user", "content": "Hello!"}],
    )
    print(response.choices[0].message.content)
    ```

=== "JavaScript"

    ```javascript
    import OpenAI from 'openai';

    const client = new OpenAI({
      baseURL: 'https://hevolve.ai/v1',
      apiKey: 'your-api-key',
    });

    const response = await client.chat.completions.create({
      model: 'hevolve',
      messages: [{ role: 'user', content: 'Hello!' }],
    });
    console.log(response.choices[0].message.content);
    ```

### Mindstory SDK (Advanced)

```javascript
import { Mindstory } from '@hertzai/mindstory';

const client = new Mindstory({ apiKey: 'your-key' });

// Chat
const response = await client.chat('Explain recursion');
console.log(response.content);
console.log(response.epistemic.confidence); // 0.95

// Multimodal
const response = await client.chat('What is this?', { image: file });

// Streaming
for await (const chunk of client.stream('Write a poem')) {
  process.stdout.write(chunk.content);
}

// Expert correction
await client.correct(
  'The capital of France is London',
  'The capital of France is Paris',
  { confidence: 0.99 }
);
```

---

## Getting an API Key

Free tier: 100 requests/day, $0 per token.

```bash
# 1. Register
curl -X POST https://hevolve.ai/api/social/register \
  -H "Content-Type: application/json" \
  -d '{"username": "dev", "email": "dev@example.com", "password": "secure"}'

# 2. Login
curl -X POST https://hevolve.ai/api/social/login \
  -H "Content-Type: application/json" \
  -d '{"username": "dev", "password": "secure"}'

# 3. Create API key
curl -X POST https://hevolve.ai/api/v1/intelligence/keys \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{"name": "website-widget", "tier": "free"}'
```

---

## See Also

- [Developer Journey](user-journey.md) — Full walkthrough from zero to deployment
- [HART SDK](sdk.md) — Native SDK for building apps on HART OS
- [Core API](../api/core.md) — `/chat` endpoint reference
