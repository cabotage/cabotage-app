from cabotage.server import db

from sqlalchemy.dialects import postgresql


class OrganizationMember(db.Model):
    __tablename__ = "organization_members"

    user_id = db.Column(
        postgresql.UUID(as_uuid=True), db.ForeignKey("users.id"), primary_key=True
    )
    organization_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("organizations.id"),
        primary_key=True,
    )
    admin = db.Column(db.Boolean, nullable=False, default=False)
    project_scope_limited = db.Column(
        db.Boolean, nullable=False, default=False, server_default="false"
    )

    user = db.relationship("User", back_populates="organizations")
    organization = db.relationship("Organization", back_populates="members")


class OrganizationTeam(db.Model):
    __tablename__ = "organization_teams"

    organization_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("organizations.id"),
        primary_key=True,
    )
    team_id = db.Column(
        postgresql.UUID(as_uuid=True), db.ForeignKey("teams.id"), primary_key=True
    )

    organization = db.relationship("Organization", back_populates="teams")
    team = db.relationship("Team", back_populates="organizations")


class TeamMember(db.Model):
    __tablename__ = "team_members"

    user_id = db.Column(
        postgresql.UUID(as_uuid=True), db.ForeignKey("users.id"), primary_key=True
    )
    team_id = db.Column(
        postgresql.UUID(as_uuid=True), db.ForeignKey("teams.id"), primary_key=True
    )
    admin = db.Column(db.Boolean, nullable=False, default=False)

    user = db.relationship("User", back_populates="teams")
    team = db.relationship("Team", back_populates="members")


class ProjectMember(db.Model):
    __tablename__ = "project_members"

    user_id = db.Column(
        postgresql.UUID(as_uuid=True), db.ForeignKey("users.id"), primary_key=True
    )
    project_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("projects.id"),
        primary_key=True,
    )

    admin = db.Column(db.Boolean, nullable=False, default=False)
    user = db.relationship("User", back_populates="project_memberships")
    project = db.relationship("Project", back_populates="members")
