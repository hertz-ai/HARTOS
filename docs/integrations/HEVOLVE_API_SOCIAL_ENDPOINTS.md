# Hevolve API - Social/Community Endpoints Documentation

**API Base URL:** `http://aws_hevolve.hertzai.com:6006`

**OpenAPI Spec Source:** Extracted from Hevolve-db-app v1.0

---

## Table of Contents
1. [Posts Endpoints](#posts-endpoints)
2. [Comments Endpoints](#comments-endpoints)
3. [Likes Endpoints](#likes-endpoints)
4. [Friends/Users Endpoints](#friendsusers-endpoints)
5. [Block/Blocking Endpoints](#blockblocking-endpoints)
6. [Schema Definitions](#schema-definitions)

---

## Posts Endpoints

### 1. Create Post
**Endpoint:** `POST /create_post`

**Summary:** Create a new post

**Request Body:**
```json
{
  "user_id": 123,
  "resource_url": "https://example.com/image.jpg",
  "content_type": "image",
  "caption": "My awesome post",
  "repost": false,
  "parent_post_id": null
}
```

**Request Schema Reference:** `CreatePost`

**Response:**
- **200 OK** - Post created successfully
- **422 Unprocessable Entity** - Validation error

**Content-Type:** `application/json`

---

### 2. Get All Posts
**Endpoint:** `POST /get-all-post`

**Summary:** Retrieve all posts (paginated)

**Parameters:** None (body request)

**Response:**
- **200 OK** - Posts retrieved successfully

**Content-Type:** `application/json`

---

### 3. Get Posts (With Filters)
**Endpoint:** `POST /get-posts`

**Summary:** Get posts with pagination and optional user filtering

**Query Parameters:**
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `page_number` | integer | No | 1 | Page number for pagination |
| `num_rows` | integer | No | 10 | Number of rows per page |
| `user_id` | integer | No | - | Filter posts by user ID |

**Response:**
- **200 OK** - Posts retrieved successfully
- **422 Unprocessable Entity** - Validation error

---

### 4. Delete Post
**Endpoint:** `POST /delete_post/{postid}`

**Summary:** Delete a post by ID

**Path Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `postid` | integer | Yes | The ID of the post to delete |

**Query Parameters:**
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `reason` | string | No | " " | Reason for deletion |

**Response:**
- **200 OK** - Post deleted successfully
- **422 Unprocessable Entity** - Validation error

---

## Comments Endpoints

### 1. Create Comment
**Endpoint:** `POST /Comment`

**Summary:** Add a comment to a post

**Request Body:**
```json
{
  "post_id": 456,
  "user_id": 123,
  "text": "Great post!",
  "parent_comment_id": null
}
```

**Request Schema Reference:** `Command`

**Response:**
- **200 OK** - Returns `CommandBase` schema
- **422 Unprocessable Entity** - Validation error

**Response Schema Example:**
```json
{
  "post_id": 456,
  "user_id": 123,
  "text": "Great post!",
  "parent_comment_id": null,
  "comment_id": 789
}
```

---

### 2. Get Comments By Post
**Endpoint:** `GET /comment_bypost`

**Summary:** Retrieve all comments for a specific post

**Query Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `post_id` | integer | Yes | The ID of the post |
| `user_id` | integer | No | Filter by user who commented |

**Response:**
- **200 OK** - Comments retrieved successfully
- **422 Unprocessable Entity** - Validation error

---

### 3. Delete Comment
**Endpoint:** `DELETE /delete_comment`

**Summary:** Delete a comment

**Query Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `comment_id` | integer | Yes | The ID of the comment to delete |
| `reason` | string | No | Reason for deletion |

**Response:**
- **200 OK** - Comment deleted successfully
- **422 Unprocessable Entity** - Validation error

---

## Likes Endpoints

### 1. Create Like
**Endpoint:** `POST /Like`

**Summary:** Like a post or comment

**Request Body:**
```json
{
  "activity_id": 789,
  "user_id": 123
}
```

**Request Schema Reference:** `Like`

**Response:**
- **200 OK** - Like created successfully
- **422 Unprocessable Entity** - Validation error

---

### 2. Get Likes By Post
**Endpoint:** `GET /like_bypost`

**Summary:** Retrieve all likes for a specific post

**Query Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `post_id` | integer | Yes | The ID of the post |

**Response:**
- **200 OK** - Likes retrieved successfully
- **422 Unprocessable Entity** - Validation error

---

## Friends/Users Endpoints

### 1. Add Friend
**Endpoint:** `POST /add_friend`

**Summary:** Send or accept a friend request

**Request Body:**
```json
{
  "user_id": 123,
  "friend_user_id": 456
}
```

**Request Schema Reference:** `add_friend`

**Response:**
- **200 OK** - Friend request processed successfully
- **422 Unprocessable Entity** - Validation error

---

### 2. Get All Users
**Endpoint:** `GET /allusers`

**Summary:** Retrieve all users with pagination

**Query Parameters:**
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `skip` | integer | No | 0 | Number of users to skip (pagination offset) |
| `limit` | integer | No | 100 | Maximum number of users to return |

**Response:**
- **200 OK** - Users list retrieved successfully
- **422 Unprocessable Entity** - Validation error

---

### 3. Get User Actions
**Endpoint:** `GET /action_by_user_id`

**Summary:** Get all actions/activities performed by a user

**Query Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_id` | integer | Yes | The ID of the user |

**Response:**
- **200 OK** - User actions retrieved successfully
- **422 Unprocessable Entity** - Validation error

---

## Block/Blocking Endpoints

### 1. Block User
**Endpoint:** `POST /block_user`

**Summary:** Block a user from viewing your profile or interacting with you

**Request Body:**
```json
{
  "user_id": 123,
  "block_user_id": 456,
  "type_of_activity": "posts",
  "scope": "global"
}
```

**Request Schema Reference:** `block_user`

**Request Fields:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | integer | Yes | The ID of the user blocking |
| `block_user_id` | integer | Yes | The ID of the user being blocked |
| `type_of_activity` | string | Yes | Type of activity to block (e.g., "posts", "comments", "messages") |
| `scope` | string | Yes | Blocking scope (e.g., "global", "posts_only") |

**Response:**
- **200 OK** - User blocked successfully
- **422 Unprocessable Entity** - Validation error

---

## Schema Definitions

### CreatePost
Used for creating new posts.

```json
{
  "title": "CreatePost",
  "type": "object",
  "required": ["user_id"],
  "properties": {
    "user_id": {
      "title": "User Id",
      "type": "integer",
      "description": "The ID of the user creating the post"
    },
    "resource_url": {
      "title": "Resource Url",
      "type": "string",
      "description": "URL to the resource (image, video, etc.)"
    },
    "content_type": {
      "title": "Content Type",
      "type": "string",
      "description": "Type of content (image, video, text, etc.)"
    },
    "caption": {
      "title": "Caption",
      "type": "string",
      "description": "Caption or text content of the post"
    },
    "repost": {
      "title": "Repost",
      "type": "boolean",
      "default": false,
      "description": "Whether this is a repost of another post"
    },
    "parent_post_id": {
      "title": "Parent Post Id",
      "type": "integer",
      "description": "ID of the original post if this is a repost"
    }
  }
}
```

---

### Command (Comment Request)
Used for creating comments on posts.

```json
{
  "title": "Command",
  "type": "object",
  "required": ["post_id", "user_id", "text"],
  "properties": {
    "post_id": {
      "title": "Post Id",
      "type": "integer",
      "description": "The ID of the post being commented on"
    },
    "user_id": {
      "title": "User Id",
      "type": "integer",
      "description": "The ID of the user commenting"
    },
    "text": {
      "title": "Text",
      "type": "string",
      "description": "The comment text"
    },
    "parent_comment_id": {
      "title": "Parent Comment Id",
      "type": "integer",
      "description": "ID of parent comment if this is a reply to another comment"
    }
  }
}
```

---

### CommandBase (Comment Response)
Response schema when a comment is created.

```json
{
  "title": "CommandBase",
  "type": "object",
  "required": ["post_id", "user_id", "text", "comment_id"],
  "properties": {
    "post_id": {
      "title": "Post Id",
      "type": "integer"
    },
    "user_id": {
      "title": "User Id",
      "type": "integer"
    },
    "text": {
      "title": "Text",
      "type": "string"
    },
    "parent_comment_id": {
      "title": "Parent Comment Id",
      "type": "integer"
    },
    "comment_id": {
      "title": "Comment Id",
      "type": "integer",
      "description": "The newly created comment's ID"
    }
  }
}
```

---

### Like (Like Request)
Used for liking posts or comments.

```json
{
  "title": "Like",
  "type": "object",
  "required": ["activity_id", "user_id"],
  "properties": {
    "activity_id": {
      "title": "Activity Id",
      "type": "integer",
      "description": "The ID of the post or comment being liked"
    },
    "user_id": {
      "title": "User Id",
      "type": "integer",
      "description": "The ID of the user liking"
    }
  }
}
```

---

### LikeBase (Like Response)
Response schema when a like is created.

```json
{
  "title": "LikeBase",
  "type": "object",
  "required": ["activity_id", "user_id", "like_id"],
  "properties": {
    "activity_id": {
      "title": "Activity Id",
      "type": "integer"
    },
    "user_id": {
      "title": "User Id",
      "type": "integer"
    },
    "like_id": {
      "title": "Like Id",
      "type": "integer",
      "description": "The newly created like's ID"
    }
  }
}
```

---

### add_friend (Friend Request)
Used for adding friends.

```json
{
  "title": "add_friend",
  "type": "object",
  "required": ["user_id", "friend_user_id"],
  "properties": {
    "user_id": {
      "title": "User Id",
      "type": "integer",
      "description": "The ID of the user initiating the friend request"
    },
    "friend_user_id": {
      "title": "Friend User Id",
      "type": "integer",
      "description": "The ID of the user to befriend"
    }
  }
}
```

---

### block_user (Block Request)
Used for blocking users.

```json
{
  "title": "block_user",
  "type": "object",
  "required": ["user_id", "block_user_id", "type_of_activity", "scope"],
  "properties": {
    "user_id": {
      "title": "User Id",
      "type": "integer",
      "description": "The ID of the user performing the block"
    },
    "block_user_id": {
      "title": "Block User Id",
      "type": "integer",
      "description": "The ID of the user being blocked"
    },
    "type_of_activity": {
      "title": "Type Of Activity",
      "type": "string",
      "description": "Type of activity to block (posts, comments, messages, etc.)"
    },
    "scope": {
      "title": "Scope",
      "type": "string",
      "description": "The scope of the block (global, posts_only, etc.)"
    }
  }
}
```

---

## Usage Summary for React Native CommunityView

The `CommunityView` React Native component would primarily use these endpoints:

### Fetching Community Feed
```javascript
// Get all posts
POST /get-all-post

// Or get posts with filters
POST /get-posts?page_number=1&num_rows=10&user_id=123
```

### Post Interactions
```javascript
// Create a post
POST /create_post

// Delete a post
POST /delete_post/{postid}?reason=optional

// Like a post
POST /Like
{ "activity_id": postId, "user_id": userId }

// Get likes for a post
GET /like_bypost?post_id=456
```

### Comments
```javascript
// Add comment to post
POST /Comment
{ "post_id": 456, "user_id": 123, "text": "...", "parent_comment_id": null }

// Get all comments for a post
GET /comment_bypost?post_id=456

// Delete a comment
DELETE /delete_comment?comment_id=789&reason=optional
```

### Social Features
```javascript
// Add friend
POST /add_friend
{ "user_id": 123, "friend_user_id": 456 }

// Block a user
POST /block_user
{ "user_id": 123, "block_user_id": 456, "type_of_activity": "posts", "scope": "global" }

// Get all users
GET /allusers?skip=0&limit=100

// Get user actions
GET /action_by_user_id?user_id=123
```

---

## Error Handling

All endpoints follow standard HTTP status codes:

- **200 OK** - Request successful
- **422 Unprocessable Entity** - Validation error (invalid parameters or schema)

Validation errors return a schema reference to `HTTPValidationError` with details about what failed validation.

---

## Notes

- All user IDs should be valid integers corresponding to registered users
- Activity IDs can refer to posts or comments depending on the context
- The `parent_post_id` and `parent_comment_id` fields enable nesting/threading of posts and comments
- The blocking system supports different activity types and scopes for granular control
- Pagination is supported on GET endpoints with `skip`/`limit` or `page_number`/`num_rows` parameters
