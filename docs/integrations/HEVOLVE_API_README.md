# Hevolve API Documentation

Complete OpenAPI documentation extracted from `http://aws_hevolve.hertzai.com:6006`

---

## Documentation Files

This extraction includes **4 comprehensive documentation files** covering all aspects of the Hevolve social/community API:

### 1. **HEVOLVE_API_SOCIAL_ENDPOINTS.md** - Complete Reference
The most detailed documentation covering:
- All 11 social endpoints with full specifications
- Request/response schemas with examples
- Query parameters and path parameters
- HTTP status codes
- Complete schema definitions for all models
- Usage summary for React Native CommunityView

**Best for:** Understanding the complete API structure and implementation details

---

### 2. **HEVOLVE_API_EXAMPLES.md** - Code Examples & Quick Start
Practical examples including:
- cURL command examples for each endpoint
- Python requests library examples
- JavaScript/Node.js fetch examples
- React hooks patterns
- React Native component examples
- Real-world usage patterns
- Error handling strategies

**Best for:** Getting started quickly with practical code examples

---

### 3. **HEVOLVE_API_SUMMARY.md** - Quick Reference Tables
Fast lookup guide with:
- Endpoint summary table (path, method, purpose, params)
- Request/response schema quick reference
- Query parameter patterns and conventions
- Status code reference
- Common workflows
- Data type reference
- Best practices
- Integration checklist

**Best for:** Quick lookups and rapid development reference

---

### 4. **HEVOLVE_API_ARCHITECTURE.md** - System Design & Data Model
Deep dive into architecture including:
- Data model diagrams (ASCII art)
- Entity relationship diagrams (ERD)
- Request/response flow diagrams
- Schema hierarchy
- API feature layers
- Validation rules
- Pagination architecture
- Performance considerations
- Potential enhancements
- CommunityView integration points

**Best for:** Understanding system design and architecture

---

## Quick Start

### For Immediate Implementation
1. Start with **HEVOLVE_API_SUMMARY.md** for endpoint overview
2. Jump to **HEVOLVE_API_EXAMPLES.md** for code samples
3. Reference **HEVOLVE_API_SOCIAL_ENDPOINTS.md** when you need details

### For Understanding Architecture
1. Read **HEVOLVE_API_ARCHITECTURE.md** data model section
2. Review entity relationships
3. Study integration points for your component

### For Complete Reference
Read **HEVOLVE_API_SOCIAL_ENDPOINTS.md** for exhaustive documentation

---

## Extracted Endpoints

### Posts (4 endpoints)
- `POST /create_post` - Create new post
- `POST /get-all-post` - Get all posts
- `POST /get-posts` - Get posts with pagination & filtering
- `POST /delete_post/{postid}` - Delete post

### Comments (3 endpoints)
- `POST /Comment` - Create comment
- `GET /comment_bypost` - Get comments for post
- `DELETE /delete_comment` - Delete comment

### Likes (2 endpoints)
- `POST /Like` - Like post/comment
- `GET /like_bypost` - Get likes for post

### Friends/Users (3 endpoints)
- `POST /add_friend` - Add friend
- `GET /allusers` - Get all users with pagination
- `GET /action_by_user_id` - Get user actions

### Blocking (1 endpoint)
- `POST /block_user` - Block user

**Total: 13 endpoints**

---

## Key Data Models

```
POST
├── user_id (creator)
├── caption (text)
├── resource_url (media URL)
├── content_type (image/video/text)
├── repost (boolean)
└── parent_post_id (for reposts)

COMMENT
├── post_id (which post)
├── user_id (who commented)
├── text (comment text)
├── parent_comment_id (nested replies)
└── comment_id (auto-generated)

LIKE
├── activity_id (post or comment ID)
├── user_id (who liked)
└── like_id (auto-generated)

FRIEND
├── user_id (requester)
└── friend_user_id (target)

BLOCK
├── user_id (blocker)
├── block_user_id (blocked)
├── type_of_activity (posts/comments/messages)
└── scope (global/partial)
```

---

## Common API Patterns

### Pagination
```javascript
// Page-based (get-posts)
POST /get-posts?page_number=1&num_rows=10

// Offset-based (allusers)
GET /allusers?skip=0&limit=100
```

### Creating Resources
```javascript
// Always POST
POST /create_post
POST /Comment
POST /Like
POST /add_friend
POST /block_user
```

### Retrieving Resources
```javascript
// GET for lists and filtered data
GET /comment_bypost?post_id=456
GET /like_bypost?post_id=456
GET /allusers
GET /action_by_user_id?user_id=123

// POST for complex queries
POST /get-posts
POST /get-all-post
```

### Deleting Resources
```javascript
// DELETE for comments
DELETE /delete_comment?comment_id=789

// POST for posts (unconventional)
POST /delete_post/{postid}
```

---

## React Native CommunityView Integration

### Essential Endpoints for CommunityView:

**Load Feed:**
```javascript
POST /get-posts?page_number=1&num_rows=10
```

**Create Post:**
```javascript
POST /create_post
```

**Like/Unlike:**
```javascript
POST /Like
```

**Comment:**
```javascript
POST /Comment
GET /comment_bypost?post_id={id}
```

**Delete Own Content:**
```javascript
POST /delete_post/{id}
DELETE /delete_comment?comment_id={id}
```

**Social Features:**
```javascript
POST /add_friend
POST /block_user
GET /allusers
```

See **HEVOLVE_API_EXAMPLES.md** for complete React Native code examples.

---

## Authentication & Security

**Status in OpenAPI Spec:** Not explicitly shown

Assumptions:
- May require Bearer token or JWT authentication
- Authorization checks (e.g., user can only delete own content)
- Rate limiting might be in place
- CORS configuration likely needed

**Recommendation:** Verify with API maintainers or through testing

---

## Response Format

All endpoints use:
- **Content-Type:** `application/json`
- **Status Codes:**
  - `200 OK` - Success
  - `422 Unprocessable Entity` - Validation error

### Success Response Example
```json
{
  "comment_id": 789,
  "post_id": 456,
  "user_id": 123,
  "text": "Great post!",
  "parent_comment_id": null
}
```

### Error Response Example
```json
{
  "detail": [
    {
      "loc": ["body", "user_id"],
      "msg": "field required",
      "type": "value_error.missing"
    }
  ]
}
```

---

## Testing Endpoints

### Using cURL
```bash
# Create post
curl -X POST "http://aws_hevolve.hertzai.com:6006/create_post" \
  -H "Content-Type: application/json" \
  -d '{"user_id":123,"caption":"Test"}'

# Get posts
curl -X POST "http://aws_hevolve.hertzai.com:6006/get-posts?page_number=1&num_rows=10" \
  -H "Content-Type: application/json"
```

### Using Postman
1. Create new POST request
2. URL: `http://aws_hevolve.hertzai.com:6006/create_post`
3. Headers: `Content-Type: application/json`
4. Body (raw JSON):
```json
{
  "user_id": 123,
  "caption": "My first post"
}
```

### Using Python
```python
import requests

response = requests.post(
    'http://aws_hevolve.hertzai.com:6006/create_post',
    json={
        'user_id': 123,
        'caption': 'Test post'
    }
)
print(response.json())
```

---

## Common Issues & Solutions

### 422 Validation Error
**Cause:** Missing required fields or invalid types
**Solution:** Check request body against schema in `HEVOLVE_API_SOCIAL_ENDPOINTS.md`

### Empty Response on POST /create_post
**Cause:** Server design (no response body)
**Solution:** Post ID might be auto-generated - check database after creation

### Resource Not Found (Implied 404/422)
**Cause:** Referenced user_id, post_id, comment_id doesn't exist
**Solution:** Verify IDs exist before making requests

### Validation Failed on delete_comment
**Cause:** comment_id doesn't exist or already deleted
**Solution:** Fetch comments first to get valid IDs

---

## Performance Tips

1. **Use Pagination**
   - Always paginate large result sets
   - Default: `page_number=1, num_rows=10`

2. **Implement Caching**
   - Cache post data locally
   - Invalidate on create/update/delete

3. **Batch Operations**
   - Consider debouncing rapid likes/comments
   - Batch multiple operations if possible

4. **Lazy Load**
   - Load comments only when expanded
   - Load user profiles on demand

5. **Infinite Scroll**
   - Increment page_number as user scrolls
   - Show loading indicator

---

## API Version Info

- **API Name:** Hevolve-db-app
- **Version:** 1.0
- **OpenAPI Version:** 3.0.2
- **Server:** http://aws_hevolve.hertzai.com:6006
- **Documentation Generated:** From official OpenAPI spec

---

## File Structure Reference

```
Documentation Files (extracted from OpenAPI spec):
│
├─ HEVOLVE_API_README.md (this file)
│  └─ Navigation and quick start guide
│
├─ HEVOLVE_API_SOCIAL_ENDPOINTS.md
│  └─ Complete detailed reference (largest file)
│
├─ HEVOLVE_API_EXAMPLES.md
│  └─ Code examples and quick start
│
├─ HEVOLVE_API_SUMMARY.md
│  └─ Quick reference tables and checklists
│
└─ HEVOLVE_API_ARCHITECTURE.md
   └─ System design and data models
```

---

## Next Steps

1. **For Development:** Start with `HEVOLVE_API_EXAMPLES.md`
2. **For Reference:** Use `HEVOLVE_API_SUMMARY.md`
3. **For Details:** Consult `HEVOLVE_API_SOCIAL_ENDPOINTS.md`
4. **For Architecture:** Review `HEVOLVE_API_ARCHITECTURE.md`

---

## Additional Resources Needed

**Consider requesting from API maintainers:**
- Authentication mechanism documentation
- Rate limiting policies
- CORS configuration details
- Media upload endpoints
- Search/filtering capabilities
- Webhook/notification system documentation
- Admin moderation endpoints

---

## Support & Documentation

**Source:** OpenAPI 3.0.2 Specification from Hevolve-db-app
**Date Extracted:** 2026-01-31
**Extracted By:** Claude Code

For issues or questions about the API, contact Hevolve platform support.

---

## License & Usage

These documentation files are extracted from public API specifications and are provided for development and integration purposes.

---

## Changelog

### Version 1.0 (2026-01-31)
- Initial extraction from OpenAPI spec
- Documented 13 social/community endpoints
- 5 comprehensive documentation files created
- Code examples for 4 languages (cURL, Python, JavaScript, React Native)
- Architecture diagrams and data model documentation

---

## Related Documentation

If you have access to these, they may provide additional context:
- Hevolve User/Authentication API docs
- Hevolve Payment/Subscription API docs
- Hevolve Messaging API docs
- Hevolve Notifications API docs
- Hevolve Admin/Moderation API docs
- Hevolve Search/Discovery API docs

This extraction focuses solely on **Community/Social Features**.
