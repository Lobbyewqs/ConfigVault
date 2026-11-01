CREATE TABLE app_user (
 id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
 username VARCHAR(64) NOT NULL UNIQUE,
 password_hash VARCHAR(100) NOT NULL,
 enabled BOOLEAN NOT NULL DEFAULT TRUE
) ENGINE=InnoDB;
CREATE TABLE config_repository (
 id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
 owner_id BIGINT NOT NULL,
 name VARCHAR(80) NOT NULL,
 game VARCHAR(20) NOT NULL DEFAULT 'CS2',
 head_version_id BIGINT NULL,
 head_revision INT NOT NULL DEFAULT 0,
 created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 UNIQUE KEY uq_owner_name(owner_id,name),
 FOREIGN KEY (owner_id) REFERENCES app_user(id)
) ENGINE=InnoDB;
CREATE TABLE config_version (
 id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
 repository_id BIGINT NOT NULL,
 revision INT NOT NULL,
 parent_id BIGINT NULL,
 restored_from_id BIGINT NULL,
 sha256 CHAR(64) NOT NULL,
 byte_size INT NOT NULL,
 encoding_label VARCHAR(16) NOT NULL,
 content_blob MEDIUMBLOB NOT NULL,
 message VARCHAR(200) NOT NULL,
 created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 UNIQUE KEY uq_repo_revision(repository_id,revision),
 FOREIGN KEY (repository_id) REFERENCES config_repository(id),
 FOREIGN KEY (parent_id) REFERENCES config_version(id),
 FOREIGN KEY (restored_from_id) REFERENCES config_version(id)
) ENGINE=InnoDB;
ALTER TABLE config_repository ADD CONSTRAINT fk_head FOREIGN KEY(head_version_id) REFERENCES config_version(id);
CREATE TABLE api_token (
 id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
 owner_id BIGINT NOT NULL,
 token_hash CHAR(64) NOT NULL UNIQUE,
 label VARCHAR(80) NOT NULL,
 expires_at TIMESTAMP(6) NOT NULL,
 revoked_at TIMESTAMP(6) NULL,
 FOREIGN KEY (owner_id) REFERENCES app_user(id)
) ENGINE=InnoDB;
CREATE TABLE idempotency_record (
 owner_id BIGINT NOT NULL,
 operation VARCHAR(32) NOT NULL,
 request_key VARCHAR(128) NOT NULL,
 request_hash CHAR(64) NOT NULL,
 version_id BIGINT NOT NULL,
 no_change BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(owner_id,operation,request_key),
 FOREIGN KEY(owner_id) REFERENCES app_user(id),
 FOREIGN KEY(version_id) REFERENCES config_version(id)
) ENGINE=InnoDB;
CREATE TABLE audit_event (
 id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
 actor_id BIGINT NOT NULL,
 action VARCHAR(32) NOT NULL,
 repository_id BIGINT NOT NULL,
 version_id BIGINT NULL,
 request_id VARCHAR(128) NULL,
 created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 KEY ix_repo_created(repository_id,created_at),
 FOREIGN KEY(actor_id) REFERENCES app_user(id),
 FOREIGN KEY(repository_id) REFERENCES config_repository(id),
 FOREIGN KEY(version_id) REFERENCES config_version(id)
) ENGINE=InnoDB;
