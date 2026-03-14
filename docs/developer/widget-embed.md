# Embedding the Hevolve Agent on Any Page

Add the Hevolve AI chat agent to any website. Three integration levels:
drop-in widget, iframe embed, or OpenAI-compatible API.

---

## Option 1: Chat Widget (Recommended)

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

## Option 2: HevolveWidget Script

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

## Option 3: iframe Embed

Embed a full chat interface in any container.

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

If your app already uses the OpenAI SDK, point it at HART OS:

=== "Python"

    ```python
    from openai import OpenAI

    client = OpenAI(
        base_url="http://localhost:6777/v1",
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
      baseURL: 'http://localhost:6777/v1',
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
curl -X POST http://localhost:6777/api/social/register \
  -H "Content-Type: application/json" \
  -d '{"username": "dev", "email": "dev@example.com", "password": "secure"}'

# 2. Login
curl -X POST http://localhost:6777/api/social/login \
  -H "Content-Type: application/json" \
  -d '{"username": "dev", "password": "secure"}'

# 3. Create API key
curl -X POST http://localhost:6777/api/v1/intelligence/keys \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{"name": "website-widget", "tier": "free"}'
```

---

## See Also

- [Developer Journey](user-journey.md) — Full walkthrough from zero to deployment
- [HART SDK](sdk.md) — Native SDK for building apps on HART OS
- [Core API](../api/core.md) — `/chat` endpoint reference
