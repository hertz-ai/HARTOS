# Hevolve API - Quick Reference & cURL Examples

**Base URL:** `http://aws_hevolve.hertzai.com:6006`

---

## Posts - Examples

### Create a Post
```bash
curl -X POST "http://aws_hevolve.hertzai.com:6006/create_post" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 123,
    "resource_url": "https://example.com/photo.jpg",
    "content_type": "image",
    "caption": "Beautiful sunset!",
    "repost": false,
    "parent_post_id": null
  }'
```

### Get All Posts
```bash
curl -X POST "http://aws_hevolve.hertzai.com:6006/get-all-post" \
  -H "Content-Type: application/json"
```

### Get Posts (Paginated)
```bash
# Page 1, 10 posts per page
curl -X POST "http://aws_hevolve.hertzai.com:6006/get-posts?page_number=1&num_rows=10" \
  -H "Content-Type: application/json"

# Filter by user
curl -X POST "http://aws_hevolve.hertzai.com:6006/get-posts?page_number=1&num_rows=10&user_id=123" \
  -H "Content-Type: application/json"
```

### Delete a Post
```bash
curl -X POST "http://aws_hevolve.hertzai.com:6006/delete_post/789?reason=spam" \
  -H "Content-Type: application/json"

# Without reason
curl -X POST "http://aws_hevolve.hertzai.com:6006/delete_post/789" \
  -H "Content-Type: application/json"
```

---

## Comments - Examples

### Create a Comment
```bash
curl -X POST "http://aws_hevolve.hertzai.com:6006/Comment" \
  -H "Content-Type: application/json" \
  -d '{
    "post_id": 456,
    "user_id": 123,
    "text": "Great post! Love the composition.",
    "parent_comment_id": null
  }'
```

### Reply to a Comment (Nested Comment)
```bash
curl -X POST "http://aws_hevolve.hertzai.com:6006/Comment" \
  -H "Content-Type: application/json" \
  -d '{
    "post_id": 456,
    "user_id": 123,
    "text": "Thanks! I appreciate the feedback.",
    "parent_comment_id": 789
  }'
```

### Get All Comments for a Post
```bash
curl -X GET "http://aws_hevolve.hertzai.com:6006/comment_bypost?post_id=456" \
  -H "Content-Type: application/json"

# Optional: filter by specific user
curl -X GET "http://aws_hevolve.hertzai.com:6006/comment_bypost?post_id=456&user_id=123" \
  -H "Content-Type: application/json"
```

### Delete a Comment
```bash
curl -X DELETE "http://aws_hevolve.hertzai.com:6006/delete_comment?comment_id=789&reason=inappropriate" \
  -H "Content-Type: application/json"

# Without reason
curl -X DELETE "http://aws_hevolve.hertzai.com:6006/delete_comment?comment_id=789" \
  -H "Content-Type: application/json"
```

---

## Likes - Examples

### Like a Post
```bash
curl -X POST "http://aws_hevolve.hertzai.com:6006/Like" \
  -H "Content-Type: application/json" \
  -d '{
    "activity_id": 456,
    "user_id": 123
  }'
```

### Get All Likes for a Post
```bash
curl -X GET "http://aws_hevolve.hertzai.com:6006/like_bypost?post_id=456" \
  -H "Content-Type: application/json"
```

---

## Friends/Users - Examples

### Add a Friend
```bash
curl -X POST "http://aws_hevolve.hertzai.com:6006/add_friend" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 123,
    "friend_user_id": 456
  }'
```

### Get All Users (Paginated)
```bash
# First 100 users (default)
curl -X GET "http://aws_hevolve.hertzai.com:6006/allusers" \
  -H "Content-Type: application/json"

# Skip 50, limit to 20
curl -X GET "http://aws_hevolve.hertzai.com:6006/allusers?skip=50&limit=20" \
  -H "Content-Type: application/json"
```

### Get User Actions/Activity
```bash
curl -X GET "http://aws_hevolve.hertzai.com:6006/action_by_user_id?user_id=123" \
  -H "Content-Type: application/json"
```

---

## Blocking - Examples

### Block a User
```bash
# Block all activities globally
curl -X POST "http://aws_hevolve.hertzai.com:6006/block_user" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 123,
    "block_user_id": 456,
    "type_of_activity": "posts",
    "scope": "global"
  }'
```

### Block Multiple Activity Types
```bash
# Block posts only
curl -X POST "http://aws_hevolve.hertzai.com:6006/block_user" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 123,
    "block_user_id": 456,
    "type_of_activity": "posts",
    "scope": "posts_only"
  }'

# Block messages only
curl -X POST "http://aws_hevolve.hertzai.com:6006/block_user" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 123,
    "block_user_id": 456,
    "type_of_activity": "messages",
    "scope": "messages_only"
  }'
```

---

## Python Examples (using requests library)

### Create a Post
```python
import requests

url = "http://aws_hevolve.hertzai.com:6006/create_post"
payload = {
    "user_id": 123,
    "resource_url": "https://example.com/photo.jpg",
    "content_type": "image",
    "caption": "Beautiful sunset!",
    "repost": False,
    "parent_post_id": None
}

response = requests.post(url, json=payload)
print(response.json())
```

### Get Posts with Pagination
```python
import requests

url = "http://aws_hevolve.hertzai.com:6006/get-posts"
params = {
    "page_number": 1,
    "num_rows": 10,
    "user_id": 123
}

response = requests.post(url, params=params)
posts = response.json()
```

### Create and Delete Comment
```python
import requests

# Create comment
create_url = "http://aws_hevolve.hertzai.com:6006/Comment"
comment_payload = {
    "post_id": 456,
    "user_id": 123,
    "text": "Nice post!",
    "parent_comment_id": None
}

create_response = requests.post(create_url, json=comment_payload)
comment_id = create_response.json().get("comment_id")

# Delete comment
delete_url = "http://aws_hevolve.hertzai.com:6006/delete_comment"
params = {"comment_id": comment_id, "reason": "spam"}
delete_response = requests.delete(delete_url, params=params)
```

### Block a User
```python
import requests

url = "http://aws_hevolve.hertzai.com:6006/block_user"
payload = {
    "user_id": 123,
    "block_user_id": 456,
    "type_of_activity": "posts",
    "scope": "global"
}

response = requests.post(url, json=payload)
```

---

## JavaScript/Node.js Examples (using fetch)

### Create a Post
```javascript
const createPost = async (userId, caption) => {
  const response = await fetch('http://aws_hevolve.hertzai.com:6006/create_post', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      user_id: userId,
      caption: caption,
      content_type: 'text',
      repost: false,
      parent_post_id: null,
    }),
  });

  return response.json();
};

// Usage
createPost(123, 'Hello world!').then(result => console.log(result));
```

### Get Posts
```javascript
const getPosts = async (pageNumber = 1, numRows = 10) => {
  const response = await fetch(
    `http://aws_hevolve.hertzai.com:6006/get-posts?page_number=${pageNumber}&num_rows=${numRows}`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
    }
  );

  return response.json();
};

// Usage
getPosts(1, 10).then(posts => console.log(posts));
```

### Like a Post
```javascript
const likePost = async (userId, postId) => {
  const response = await fetch('http://aws_hevolve.hertzai.com:6006/Like', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      activity_id: postId,
      user_id: userId,
    }),
  });

  return response.json();
};

// Usage
likePost(123, 456).then(like => console.log(like));
```

### Add Comment
```javascript
const addComment = async (userId, postId, text, parentCommentId = null) => {
  const response = await fetch('http://aws_hevolve.hertzai.com:6006/Comment', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      post_id: postId,
      user_id: userId,
      text: text,
      parent_comment_id: parentCommentId,
    }),
  });

  return response.json();
};

// Usage
addComment(123, 456, 'Great post!').then(comment => console.log(comment));
```

### Get Comments for a Post
```javascript
const getComments = async (postId) => {
  const response = await fetch(
    `http://aws_hevolve.hertzai.com:6006/comment_bypost?post_id=${postId}`,
    {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
      },
    }
  );

  return response.json();
};

// Usage
getComments(456).then(comments => console.log(comments));
```

### React Hooks Pattern
```javascript
import { useState, useEffect } from 'react';

const useCommunityPosts = (userId) => {
  const [posts, setPosts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchPosts = async () => {
      try {
        const response = await fetch(
          'http://aws_hevolve.hertzai.com:6006/get-posts?page_number=1&num_rows=10',
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
          }
        );
        const data = await response.json();
        setPosts(data);
      } catch (err) {
        setError(err);
      } finally {
        setLoading(false);
      }
    };

    fetchPosts();
  }, []);

  return { posts, loading, error };
};
```

---

## React Native Examples (for CommunityView component)

### Fetch Posts in Component
```javascript
import React, { useState, useEffect } from 'react';
import { View, FlatList, Text } from 'react-native';

const CommunityView = ({ userId }) => {
  const [posts, setPosts] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchPosts();
  }, []);

  const fetchPosts = async () => {
    try {
      const response = await fetch(
        'http://aws_hevolve.hertzai.com:6006/get-posts?page_number=1&num_rows=10',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
        }
      );
      const data = await response.json();
      setPosts(data);
    } catch (error) {
      console.error('Error fetching posts:', error);
    } finally {
      setLoading(false);
    }
  };

  return (
    <View style={{ flex: 1 }}>
      <FlatList
        data={posts}
        renderItem={({ item }) => (
          <View>
            <Text>{item.caption}</Text>
          </View>
        )}
        keyExtractor={(item) => item.id.toString()}
        onEndReached={loadMore}
      />
    </View>
  );
};
```

### Like/Comment in React Native
```javascript
const handleLike = async (postId) => {
  try {
    const response = await fetch('http://aws_hevolve.hertzai.com:6006/Like', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        activity_id: postId,
        user_id: currentUserId,
      }),
    });
    const result = await response.json();
    // Update UI with new like
    updatePostLikeCount(postId, result);
  } catch (error) {
    console.error('Error liking post:', error);
  }
};

const handleComment = async (postId, commentText) => {
  try {
    const response = await fetch('http://aws_hevolve.hertzai.com:6006/Comment', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        post_id: postId,
        user_id: currentUserId,
        text: commentText,
        parent_comment_id: null,
      }),
    });
    const result = await response.json();
    // Add new comment to UI
    updateComments(postId, result);
  } catch (error) {
    console.error('Error adding comment:', error);
  }
};
```

---

## HTTP Status Codes & Error Handling

| Code | Status | Description |
|------|--------|-------------|
| 200 | OK | Request successful |
| 422 | Unprocessable Entity | Validation error in request body or parameters |

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

## Performance Tips

1. **Pagination**: Always use pagination when fetching posts/users
   ```
   GET /get-posts?page_number=1&num_rows=10
   ```

2. **Infinite Scroll**: Increment page_number as user scrolls
   ```javascript
   const loadMore = () => {
     setPageNumber(prev => prev + 1);
     // fetch with new page number
   };
   ```

3. **Caching**: Cache user data and posts locally when possible

4. **Lazy Loading**: Load comments only when user expands a post

5. **Batch Operations**: Consider caching likes/comments before syncing

---

## API Authentication Notes

*Note: The current OpenAPI spec doesn't show authentication requirements. Check if Bearer tokens or other auth headers are needed.*

Potential auth header pattern:
```bash
curl -X GET "http://aws_hevolve.hertzai.com:6006/allusers" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json"
```
