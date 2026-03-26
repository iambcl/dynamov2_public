"""
Database models for the GitHub repository collector.

This module contains SQLAlchemy models for storing collected GitHub repository data.
"""

from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, ForeignKey, Table, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import JSONB, ARRAY


Base = declarative_base()


# Association tables for many-to-many relationships
repository_application_labels = Table(
    'repository_application_labels',
    Base.metadata,
    Column('repository_id', Integer, ForeignKey('github_repositories.id'), primary_key=True),
    Column('label_id', Integer, ForeignKey('application_labels.id'), primary_key=True),
    Column('confidence', Float),
    comment="Association table linking repositories to application labels"
)

class ApplicationLabel(Base):
    """
    Model for storing application labels that can be assigned to repositories.
    
    This table stores different types of application categories or classifications
    that can be assigned to GitHub repositories (e.g., 'web-app', 'database', 'api', etc.)
    """
    
    __tablename__ = 'application_labels'
    
    id = Column(Integer, primary_key=True, autoincrement=True, comment="Unique identifier for the application label")
    name = Column(String(100), nullable=False, unique=True, comment="Name of the application label")
    description = Column(Text, nullable=True, comment="Description of what this label represents")
    
    # Relationships
    repositories = relationship(
        "GitHubRepository",
        secondary=repository_application_labels,
        back_populates="application_labels"
    )
    
    def __repr__(self) -> str:
        return f"<ApplicationLabel(name='{self.name}')>"
    
    def __str__(self) -> str:
        return self.name


class GitHubRepository(Base):
  """
  Model for storing GitHub repository information collected during crawling.
  
  This table stores comprehensive information about GitHub repositories that
  contain docker-compose files or other containerization configurations.
  """
  
  __tablename__ = 'github_repositories'
  
  # Primary key
  id = Column(Integer, primary_key=True, autoincrement=True, comment="Unique identifier for the repository record")
  
  # Repository identification
  name = Column(String(255), nullable=False, comment="Repository name (e.g., 'blockscout/blockscout')")
  url = Column(String(500), nullable=False, unique=True, comment="Full GitHub repository URL")
  
  # Repository metadata
  about = Column(Text, nullable=True, comment="Repository description/about text")
  created_at = Column(DateTime, nullable=True, comment="When the repository was created on GitHub")
  last_commit = Column(DateTime, nullable=True, comment="Timestamp of the last commit to the repository")
  
  # Repository statistics
  num_stars = Column(Integer, default=0, nullable=True, comment="Number of stars the repository has")
  num_issues = Column(Integer, default=0, nullable=True, comment="Number of open issues")
  
  # Docker/Container related information
  num_containers = Column(Integer, default=0, nullable=True, comment="Number of containers defined in docker-compose files")
  docker_compose_commands = Column(JSONB, default={}, nullable=True, comment="commands required to run the docker containers. NULL to indicate containers cannot be brought up")
  cleaned_docker_compose_filepath = Column(ARRAY(String), nullable=True, comment="Path(s) to cleaned docker compose files within repository")

  # Repository quality indicators
  readme = Column(Boolean, nullable=True, comment="Whether the repository has a README file.")
    
  # Audit fields
  crawled_at = Column(DateTime, default=func.now(), nullable=True, comment="When this record was crawled/created")
  updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=True, comment="When this record was last updated")
  
  # Additional field to support processing
  docker_compose_filepath = Column(ARRAY(String),nullable=True,comment="Path to cleaned docker compose file within repository")

  # Relationships
  application_labels = relationship(
      "ApplicationLabel",
      secondary=repository_application_labels,
      back_populates="repositories"
  )
  
  traffic_parameters = relationship(
      "TrafficParameters",
      back_populates="repository",
      uselist=False
  )
  agent_traffic_parameters = relationship(
      "AgentTrafficParameters",
      back_populates="repository",
      uselist=False
  )

  agent_run_results = relationship("AgentRunResult", back_populates="repository", cascade="all, delete-orphan")
    
  def __repr__(self) -> str:
      """String representation of the GitHubRepository object."""
      return f"<GitHubRepository(name='{self.name}', stars={self.num_stars}, containers={self.num_containers})>"
  
  def __str__(self) -> str:
      """Human-readable string representation."""
      return f"{self.name} ({self.num_stars} stars, {self.num_containers} containers)"

class TrafficParameters(Base):
    """
    Model for storing network traffic characteristics captured for a repository.

    Each record is tied to a single repository (1:1) and stores analysis outputs
    such as protocol distribution, time series metrics, burstiness measurements,
    and optional failure notes when captures do not complete successfully.
    """

    __tablename__ = 'traffic_parameters'

    id = Column(Integer, ForeignKey("github_repositories.id"), primary_key=True, autoincrement=True, comment="Unique identifier for the traffic parameters")
    application_flow = Column(JSONB, nullable=True, comment="Traffic make up based on application")
    failure_reason = Column(Text, nullable=True, comment="Reason traffic capture or analysis failed, if applicable")
    one_minute_check = Column(Boolean, comment="Keep track of whether this repository passed the one-minute check")
    subnets = Column(JSONB, comment="List of subnets observed in this repository.")
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=True, comment="When this record was last updated")
    processing_host = Column(Text, comment="Which host processed this repository")
    application_traffic_present = Column(Text, comment="Does LLM think application traffic is present")
    reason = Column(Text, comment="Why does LLM think application traffic is present")
    application = Column(Text, comment="Application mapping")


    #To be removed but keeping here for original DB
    upload_time_series = Column(ARRAY(Float), nullable=True, comment="Traffic time series (upload)")
    download_time_series = Column(ARRAY(Float), nullable=True, comment="Traffic time series (download)")
    traffic_time_series = Column(ARRAY(Float), nullable=True, comment="Traffic time series (upload and download)")
    wave = Column(ARRAY(Float), nullable=True, comment="Burstiness metric: wave")
    on_off = Column(ARRAY(Float), nullable=True, comment="Burstiness metric: on-off")
    
    # Relationships
    repository = relationship(
        "GitHubRepository",
        back_populates="traffic_parameters"
    )

    def __repr__(self) -> str:
        return f"<TrafficParameters(id='{self.id}')>"

    def __str__(self) -> str:
        return str(self.id)


class AgentTrafficParameters(Base):
    """
    Model for storing agent-run network traffic characteristics for a repository.

    Mirrors the structure of TrafficParameters so agent workflows can persist
    their own analysis artifacts without mutating the primary pipeline records.
    """

    __tablename__ = 'agent_traffic_parameters'

    id = Column(Integer, ForeignKey("github_repositories.id"), primary_key=True,  comment="Repository identifier for the traffic parameters")
    model = Column(String(255), primary_key=True, comment="Model or variant identifier for this agent run")
    run_id = Column(Integer, primary_key=True, nullable=False, comment="Run identifier for this agent traffic record")
    application_flow = Column(JSONB, nullable=True, comment="Traffic make up based on application")
    failure_reason = Column(Text, nullable=True, comment="Reason traffic capture or analysis failed, if applicable")
    one_minute_check = Column(Boolean, comment="Keep track of whether this repository passed the one-minute check")
    subnets = Column(JSONB, comment="List of subnets observed in this repository.")
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=True, comment="When this record was last updated")
    processing_host = Column(Text, comment="Which host processed this repository")
    application_traffic_present = Column(Text, comment="Does LLM think application traffic is present")
    reason = Column(Text, comment="Why does LLM think application traffic is present")
    application = Column(Text, comment="Application mapping")
    #To be removed but keeping here for original DB
    # upload_time_series = Column(ARRAY(Float), nullable=True, comment="Traffic time series (upload)")
    # download_time_series = Column(ARRAY(Float), nullable=True, comment="Traffic time series (download)")
    # traffic_time_series = Column(ARRAY(Float), nullable=True, comment="Traffic time series (upload and download)")
    # wave = Column(ARRAY(Float), nullable=True, comment="Burstiness metric: wave")
    # on_off = Column(ARRAY(Float), nullable=True, comment="Burstiness metric: on-off")


    repository = relationship(
        "GitHubRepository",
        back_populates="agent_traffic_parameters"
    )

    def __repr__(self) -> str:
        return f"<AgentTrafficParameters(id='{self.id}')>"

    def __str__(self) -> str:
        return str(self.id)

class AgentRunResult(Base):
    """
    Stores the outputs of `run_agents` executions for a repository.

    Captures normalized environment and codex agent results, their latencies,
    and the raw payloads for debugging or replay. Primary key includes
    repository_id, model, and run_id to allow multiple runs per repository/model.
    """

    __tablename__ = 'agent_run_results'

    repository_id = Column(Integer, ForeignKey("github_repositories.id", ondelete="CASCADE"), primary_key=True, comment="Repository associated with this agent run")
    model = Column(String(255), primary_key=True, comment="Model or variant identifier for this agent run")
    run_id = Column(Integer, primary_key=True, nullable=False, comment="Unique identifier for this agent run")

    # Environment agent details
    env_status = Column(Boolean, comment="Status returned by environment agent")
    env_environmental_variables = Column(JSONB, comment="Environmental variables added by the environment agent")
    env_location = Column(Text, comment="Location of the generated .env file")
    env_latency_seconds = Column(Float, comment="Latency in seconds for the environment agent response")

    # Codex agent details
    codex_working = Column(Boolean, comment="Whether codex agent determined the compose works")
    codex_steps_taken = Column(ARRAY(Text), comment="Steps taken by codex agent")
    codex_latency_seconds = Column(Float, comment="Latency in seconds for the codex agent response")

    # Raw payloads for audit/debugging
    raw_env_result = Column(JSONB, comment="Raw environment agent result payload")
    raw_codex_result = Column(JSONB, comment="Raw codex agent result payload")
    notes = Column(Text, comment="Additional notes or context for this agent run")

    updated_at = Column(
        DateTime,
        default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="When this run result was last updated",
    )

    repository = relationship("GitHubRepository", back_populates="agent_run_results")

    def __repr__(self) -> str:
        return (
            f"<AgentRunResult(repository_id={self.repository_id}, model='{self.model}', "
            f"env_status={self.env_status}, codex_working={self.codex_working})>"
        )

    def __str__(self) -> str:
        return f"{self.repository_id}:{self.model}"
