# Hevolve API - Architecture & Data Model

**API Base URL:** `http://aws_hevolve.hertzai.com:6006`

---

## Data Model Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                          USER (Primary)                         │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ id: integer                                              │  │
│  │ Can: Create Posts, Comments, Likes, Add Friends, Block  │  │
│  └──────────────────────────────────────────────────────────┘  │
└──────────────┬───────────────────────────────────────────────────┘
               │
        ┌──────┴──────┬──────────┬────────────┐
        │             │          │            │
        ▼             ▼          ▼            ▼
    ┌───────┐  ┌──────────┐  ┌────────┐  ┌─────────┐
    │ Posts │  │ Comments │  │ Likes  │  │ Friends │
    └─┬─────┘  └────┬─────┘  └────┬───┘  └────┬────┘
      │             │             │           │
      │  1:N        │  1:N        │  1:N     │  1:N
      │        ┌────┴──────┐  ┌───┴────┐    │
      │        │ Comments  │  │ Likes  │    │
      │        │ (nested)  │  │(on post)   │
      │        └───────────┘  └────────┘    │
      │                                     │
      └─────────────────────────────────────┘
              Post Activity Graph


┌──────────────────────────┐
│   Blocking System        │
├──────────────────────────┤
│ Blocker → Blocked User   │
│ Type: posts/comments/msg │
│ Scope: global/partial    │
└──────────────────────────┘
```

---

## Entity Relationship Diagram (ERD)

```
USER
├── id (PK)
├── user_id (foreign key)
└── [other user fields]

POST
├── id (PK)
├── user_id (FK) → USER
├── resource_url
├── content_type
├── caption
├── repost (boolean)
├── parent_post_id (FK) → POST (self-reference for reposts)
└── created_at (inferred)

COMMENT
├── comment_id (PK)
├── post_id (FK) → POST
├── user_id (FK) → USER
├── text
├── parent_comment_id (FK) → COMMENT (self-reference for nested replies)
└── created_at (inferred)

LIKE
├── like_id (PK)
├── activity_id (FK) → POST or COMMENT
├── user_id (FK) → USER
└── created_at (inferred)

FRIEND
├── user_id (FK) → USER
├── friend_user_id (FK) → USER
└── status (inferred: pending/accepted/rejected)

BLOCK
├── user_id (FK) → USER (blocker)
├── block_user_id (FK) → USER (blocked)
├── type_of_activity (string)
├── scope (string)
└── created_at (inferred)
```

---

## API Request/Response Flow

```
CLIENT REQUEST
       │
       ├─ Headers: Content-Type: application/json
       │
       ├─ Method: GET | POST | DELETE
       │
       ├─ Body (if applicable):
       │  └─ JSON payload matching Schema
       │
       ▼
    HEVOLVE API SERVER
       │
       ├─ Route matching
       │
       ├─ Parameter validation
       │
       ├─ Schema validation
       │
       ├─ Business logic
       │
       ├─ Database operations
       │
       ▼
    RESPONSE
       │
       ├─ Status Code: 200 | 422
       │
       ├─ Headers: Content-Type: application/json
       │
       └─ Body: JSON response
           │
           ├─ Success (200): Data returned
           │
           └─ Error (422): Validation error details
```

---

## Schema Hierarchy

```
API
├── Paths
│   ├── /create_post
│   │   └── RequestBody: CreatePost
│   │       └── Response: 200 OK (empty schema)
│   │
│   ├── /Comment
│   │   ├── RequestBody: Command
│   │   └── Response: 200 CommandBase
│   │
│   ├── /Like
│   │   ├── RequestBody: Like
│   │   └── Response: 200 LikeBase
│   │
│   ├── /add_friend
│   │   ├── RequestBody: add_friend
│   │   └── Response: 200 OK
│   │
│   └── /block_user
│       ├── RequestBody: block_user
│       └── Response: 200 OK
│
├── Components
│   ├── Schemas
│   │   ├── CreatePost
│   │   │   ├── user_id: integer (required)
│   │   │   ├── resource_url: string (optional)
│   │   │   ├── content_type: string (optional)
│   │   │   ├── caption: string (optional)
│   │   │   ├── repost: boolean (default: false)
│   │   │   └── parent_post_id: integer (optional)
│   │   │
│   │   ├── Command (Comment Request)
│   │   │   ├── post_id: integer (required)
│   │   │   ├── user_id: integer (required)
│   │   │   ├── text: string (required)
│   │   │   └── parent_comment_id: integer (optional)
│   │   │
│   │   ├── CommandBase (Comment Response)
│   │   │   ├── post_id: integer
│   │   │   ├── user_id: integer
│   │   │   ├── text: string
│   │   │   ├── parent_comment_id: integer
│   │   │   └── comment_id: integer (auto-generated)
│   │   │
│   │   ├── Like
│   │   │   ├── activity_id: integer (required)
│   │   │   └── user_id: integer (required)
│   │   │
│   │   ├── LikeBase
│   │   │   ├── activity_id: integer
│   │   │   ├── user_id: integer
│   │   │   └── like_id: integer (auto-generated)
│   │   │
│   │   ├── add_friend
│   │   │   ├── user_id: integer (required)
│   │   │   └── friend_user_id: integer (required)
│   │   │
│   │   └── block_user
│   │       ├── user_id: integer (required)
│   │       ├── block_user_id: integer (required)
│   │       ├── type_of_activity: string (required)
│   │       └── scope: string (required)
│   │
│   └── Responses
│       ├── 200: Successful
│       └── 422: Validation Error
```

---

## API Feature Layers

```
┌─────────────────────────────────────────────────────┐
│           APPLICATION LAYER (React Native)          │
│  ┌───────────────────────────────────────────────┐  │
│  │ CommunityView | ProfileView | FeedView | etc  │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────┬───────────────────────────────┘
                      │ HTTP/JSON
┌─────────────────────▼───────────────────────────────┐
│           API GATEWAY LAYER                         │
│  Base URL: http://aws_hevolve.hertzai.com:6006     │
│  ┌───────────────────────────────────────────────┐  │
│  │ Route Matching | Auth* | Rate Limiting* | etc │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────┬───────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────┐
│        BUSINESS LOGIC LAYER                         │
│  ┌─────────────┬──────────────┬──────────────────┐  │
│  │ Post Service│Comment Service│Social Service   │  │
│  │ ├ Create    │ ├ Create      │ ├ Friend       │  │
│  │ ├ Read      │ ├ Read        │ ├ Block        │  │
│  │ ├ Delete    │ ├ Delete      │ └ Activity     │  │
│  │ └ Fetch     │ └ Fetch       │                │  │
│  │             │               │                │  │
│  │ Like Service│               │                │  │
│  │ ├ Create    │               │                │  │
│  │ ├ Delete    │               │                │  │
│  │ └ Fetch     │               │                │  │
│  └─────────────┴──────────────┴──────────────────┘  │
└─────────────────────┬───────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────┐
│         DATABASE LAYER                              │
│  ┌─────────┬──────────┬────────┬──────────────────┐ │
│  │users    │posts     │comments│likes | friends  │ │
│  │         │          │        │blocks           │ │
│  └─────────┴──────────┴────────┴──────────────────┘ │
└─────────────────────────────────────────────────────┘

* Assumed features not explicitly shown in OpenAPI
```

---

## Content Types Supported

### CreatePost content_type Values (Inferred)
```
- "text"        : Text-only posts
- "image"       : Posts with image
- "video"       : Posts with video
- "article"     : Shared article
- "link"        : External link
- "mixed"       : Multiple media types
```

---

## Activity Types & Scopes (Blocking System)

### type_of_activity Values
```
- "posts"       : Limit post visibility/interaction
- "comments"    : Prevent commenting
- "messages"    : Prevent direct messaging (inferred)
- "all"         : Block all interaction
```

### scope Values
```
- "global"           : Apply block everywhere
- "posts_only"       : Block specific to posts
- "comments_only"    : Block specific to comments
- "messages_only"    : Block specific to messaging
- "partial"          : Limited scope (implementation-specific)
```

---

## Request/Response Cycle Examples

### Example 1: Creating a Post
```
REQUEST:
  POST /create_post HTTP/1.1
  Host: aws_hevolve.hertzai.com:6006
  Content-Type: application/json

  {
    "user_id": 123,
    "caption": "Beautiful day!",
    "content_type": "image",
    "resource_url": "https://cdn.example.com/photo.jpg",
    "repost": false,
    "parent_post_id": null
  }

RESPONSE:
  HTTP/1.1 200 OK
  Content-Type: application/json

  { }  ← Empty response (ID likely returned or generated by database)
```

### Example 2: Adding a Comment
```
REQUEST:
  POST /Comment HTTP/1.1
  Host: aws_hevolve.hertzai.com:6006
  Content-Type: application/json

  {
    "post_id": 456,
    "user_id": 123,
    "text": "Nice post!",
    "parent_comment_id": null
  }

RESPONSE:
  HTTP/1.1 200 OK
  Content-Type: application/json

  {
    "comment_id": 789,        ← Server-generated
    "post_id": 456,
    "user_id": 123,
    "text": "Nice post!",
    "parent_comment_id": null
  }
```

### Example 3: Liking a Post
```
REQUEST:
  POST /Like HTTP/1.1
  Host: aws_hevolve.hertzai.com:6006
  Content-Type: application/json

  {
    "activity_id": 456,       ← Post ID
    "user_id": 123
  }

RESPONSE:
  HTTP/1.1 200 OK
  Content-Type: application/json

  {
    "like_id": 999,           ← Server-generated
    "activity_id": 456,
    "user_id": 123
  }
```

### Example 4: Getting Comments for Post
```
REQUEST:
  GET /comment_bypost?post_id=456 HTTP/1.1
  Host: aws_hevolve.hertzai.com:6006
  Content-Type: application/json

RESPONSE:
  HTTP/1.1 200 OK
  Content-Type: application/json

  [
    {
      "comment_id": 789,
      "post_id": 456,
      "user_id": 123,
      "text": "Nice post!",
      "parent_comment_id": null
    },
    {
      "comment_id": 790,
      "post_id": 456,
      "user_id": 124,
      "text": "I agree!",
      "parent_comment_id": null
    }
  ]
```

---

## Validation Rules (Inferred)

### CreatePost
- `user_id`: Required, must be valid integer, must reference existing user
- `caption`: Optional, reasonable length limit (5000 chars?)
- `resource_url`: Optional, valid URL format
- `content_type`: Optional, valid content type
- `repost`: Optional boolean
- `parent_post_id`: Optional, if provided must reference existing post

### Command (Comment)
- `post_id`: Required, must reference existing post
- `user_id`: Required, must reference existing user
- `text`: Required, must be non-empty, reasonable length
- `parent_comment_id`: Optional, if provided must reference existing comment on same post

### Like
- `activity_id`: Required, must reference post or comment
- `user_id`: Required, must reference existing user
- Duplicate prevention: Same user can't like same activity twice (inferred)

### add_friend
- `user_id`: Required, must reference existing user
- `friend_user_id`: Required, must reference existing user
- Must be different users (user_id ≠ friend_user_id, inferred)

### block_user
- `user_id`: Required, blocker's user ID
- `block_user_id`: Required, user being blocked
- `type_of_activity`: Required, valid activity type
- `scope`: Required, valid scope value
- Must be different users

---

## Pagination Architecture

### Offset-Based Pagination (allusers)
```
GET /allusers?skip=0&limit=100
  │
  ├─ skip: Start position (0-indexed)
  ├─ limit: Number of results
  │
  ▼
Database Query: SELECT * FROM users OFFSET 0 LIMIT 100
  │
  ▼
Returns: Array of 100 user objects
```

### Page-Based Pagination (get-posts)
```
POST /get-posts?page_number=1&num_rows=10
  │
  ├─ page_number: 1-indexed page
  ├─ num_rows: Results per page
  │
  ▼
Database Query:
  skip = (page_number - 1) * num_rows
  SELECT * FROM posts OFFSET skip LIMIT num_rows
  │
  ▼
Returns: Array of 10 post objects
```

---

## Status & HTTP Codes

```
200 OK
├─ Request processed successfully
├─ Data returned or operation completed
└─ Used by all successful responses

422 Unprocessable Entity
├─ Schema validation error
├─ Invalid parameter types
├─ Missing required fields
├─ Invalid field values
└─ Non-existent resource references
    └─ Could also be 404 but API uses 422
```

---

## Security Considerations (Missing in Spec)

The OpenAPI spec doesn't show:
- Authentication mechanism (Bearer token, JWT, etc.)
- Authorization checks (user can only delete own content?)
- Rate limiting headers
- CORS configuration
- Input sanitization strategies

These should be verified from additional documentation or API testing.

---

## Performance Optimization Areas

### 1. Query Optimization
- **N+1 Problem**: Fetching post likes might query each user separately
- **Solution**: Use JOIN queries to fetch related data

### 2. Caching Strategy
- **Posts**: Can cache frequently accessed posts
- **Users**: Cache user profiles
- **Likes/Comments**: Less cacheable (frequently changing)

### 3. Pagination Efficiency
- Offset-based pagination is simple but slow for large offsets
- Cursor-based pagination would be better for large datasets

### 4. Connection Pooling
- Database connections should be pooled for efficiency

### 5. API Response Compression
- gzip compression for JSON responses

---

## Potential Enhancements (Not in Current API)

```
Missing Features:
├─ Search functionality
├─ Feed algorithm/recommendations
├─ Notifications
├─ Real-time updates (WebSocket?)
├─ Media upload endpoint
├─ Hashtags
├─ Mentions
├─ Trending posts
├─ User following (only friends shown)
├─ Edit post/comment
├─ Draft posts
├─ Post scheduling
├─ Analytics
├─ Admin moderation tools
└─ Two-factor authentication
```

---

## Deployment Architecture (Inferred)

```
Internet
  │
  ▼
Load Balancer
  │
  ├─ Instance 1: API Server
  │  └─ Python/FastAPI (likely, given OpenAPI generation)
  │
  ├─ Instance 2: API Server
  │
  └─ Instance N: API Server
        │
        ▼
    Database Server
    └─ PostgreSQL / MySQL / etc.
        │
        └─ Cache Layer (Redis?)
            └─ For sessions, rate limiting, caching
```

---

## Integration Points for CommunityView

```
CommunityView Component
├─ useEffect (componentDidMount)
│  └─ POST /get-posts → Load initial feed
│
├─ FlatList / ScrollView
│  ├─ onEndReached
│  │  └─ POST /get-posts?page_number={n+1} → Load more posts
│  │
│  └─ Render Post Item
│     ├─ Like button
│     │  └─ POST /Like → Send like
│     │
│     ├─ Comment button
│     │  └─ POST /Comment → Add comment
│     │
│     ├─ Share button
│     │  └─ POST /create_post (repost=true) → Repost
│     │
│     ├─ Delete button (if owner)
│     │  └─ POST /delete_post/{id} → Delete
│     │
│     ├─ Block button
│     │  └─ POST /block_user → Block user
│     │
│     └─ User profile link
│        └─ GET /action_by_user_id → User activity
│
├─ Comments Modal/View
│  ├─ GET /comment_bypost → Load comments
│  ├─ POST /Comment → Add reply
│  └─ DELETE /delete_comment → Remove comment
│
└─ Create Post Modal
   ├─ Input validation
   ├─ Image/media selection
   └─ POST /create_post → Submit new post
```

---

## Summary

The Hevolve API provides a comprehensive social networking backend with:
- **Post Management**: Create, view, delete posts with nested reposts
- **Comments**: Thread-based nested comments
- **Likes**: Activity-based liking system
- **Social Graph**: Friends and blocking
- **Pagination**: For efficient data loading
- **Simple Architecture**: RESTful HTTP/JSON API

The API is designed for building social platforms like Twitter, Instagram, or community forums with engagement features (likes, comments) and social connections (friends, blocking).
