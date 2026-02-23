-- Social Feed System — Database Schema
-- Compatible with MySQL 8.0 and TiDB 7.x+
-- Run once at startup (docker-entrypoint-initdb.d or manually).

CREATE DATABASE IF NOT EXISTS social_feed;
USE social_feed;

-- ── Users ─────────────────────────────────────────────────────────────────
-- Stores user profiles. interest_vector is maintained by the embedding-worker
-- as the user interacts with content; initially seeded randomly by the API.
CREATE TABLE IF NOT EXISTS users (
    user_id      VARCHAR(36)  NOT NULL PRIMARY KEY,
    username     VARCHAR(100) NOT NULL,
    display_name VARCHAR(255),
    interest_vector JSON,                    -- list[float], 384-dim
    created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE INDEX idx_username (username)
);

-- ── Social Graph (Follows) ────────────────────────────────────────────────
-- Directed edge: follower_id → followee_id.
-- Fan-out worker reads idx_followee to distribute new posts.
CREATE TABLE IF NOT EXISTS follows (
    follower_id  VARCHAR(36) NOT NULL,
    followee_id  VARCHAR(36) NOT NULL,
    created_at   TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (follower_id, followee_id),
    INDEX idx_followee (followee_id)         -- "who follows user X?"
);

-- ── Posts ─────────────────────────────────────────────────────────────────
-- Metadata only. Actual media bytes are in MinIO (media_key = object key).
CREATE TABLE IF NOT EXISTS posts (
    post_id    VARCHAR(36)  NOT NULL PRIMARY KEY,
    user_id    VARCHAR(36)  NOT NULL,
    content    TEXT,
    media_key  VARCHAR(500),               -- MinIO object key
    media_type VARCHAR(20),               -- 'image' | 'video' | NULL
    like_count INT          NOT NULL DEFAULT 0,
    created_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_posts_user    (user_id),
    INDEX idx_posts_created (created_at)
);

-- ── Likes ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS likes (
    user_id    VARCHAR(36) NOT NULL,
    post_id    VARCHAR(36) NOT NULL,
    created_at TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, post_id)
);
