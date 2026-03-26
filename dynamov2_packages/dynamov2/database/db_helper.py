"""
Database helper functions for CRUD operations.

This module provides convenient functions for adding and updating records
in the GitHub repository collector database.
"""

import os
import sys
import json
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
import re
from sqlalchemy import create_engine, or_, func, select
from sqlalchemy.orm import sessionmaker, Session, aliased
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from dynamov2.utils.util import _normalize_result

# Add project root to path for imports
current_dir = os.path.abspath(os.path.dirname(__file__))
project_root = os.path.abspath(os.path.join(current_dir, '..', '..'))
sys.path.append(project_root)

from ..logger.logger import CustomLogger
from .models import (
    Base,
    GitHubRepository,
    ApplicationLabel,
    TrafficParameters,
    AgentTrafficParameters,
    AgentRunResult,
    repository_application_labels
)

# Database configuration
POSTGRES_USER = os.getenv("POSTGRES_USER")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
POSTGRES_DB = os.getenv("POSTGRES_DB")
POSTGRES_CONTAINER = os.getenv("POSTGRES_CONTAINER")
POSTGRES_PORT = os.getenv("POSTGRES_PORT")

if not all([POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, POSTGRES_CONTAINER, POSTGRES_PORT]):
    raise ValueError("Missing required PostgreSQL environment variables")

DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_CONTAINER}:{POSTGRES_PORT}/{POSTGRES_DB}"

# Create engine and session factory
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

logger = CustomLogger("DB_HELPER", logfile_name="DB_HELPER")


class DatabaseHelper:
    """Helper class for database operations."""
    
    def __init__(self):
        self.engine = engine
        self.SessionLocal = SessionLocal
    
    def get_session(self) -> Session:
        """Get a new database session."""
        return self.SessionLocal()

    def get_ids_from_table(self, table: Any, limit: Optional[int] = None) -> List[int]:
        """
        Return a list of repository IDs for the provided table.

        Args:
            table: SQLAlchemy model class, Table object, or table name.
            limit: Optional limit on the number of IDs to return.
        """
        session = self.get_session()
        try:
            if isinstance(table, str):
                table_obj = Base.metadata.tables.get(table)
                if table_obj is None:
                    raise ValueError(f"Table '{table}' not found in metadata")
                id_column = table_obj.c.get("repository_id")
            else:
                table_obj = table
                id_column = None
                if hasattr(table_obj, "__table__") and "repository_id" in table_obj.__table__.c:
                    id_column = table_obj.__table__.c.repository_id
                elif hasattr(table_obj, "c") and "repository_id" in table_obj.c:
                    id_column = table_obj.c.repository_id
                elif hasattr(table_obj, "repository_id") and hasattr(table_obj.repository_id, "expression"):
                    id_column = table_obj.repository_id

            if id_column is None:
                raise ValueError("Provided table does not expose a 'repository_id' column")

            query = select(id_column).distinct().order_by(id_column.asc())
            if limit is not None:
                query = query.limit(limit)

            return list(session.execute(query).scalars().all())
        except SQLAlchemyError as e:
            logger.error(f"❌ Database error fetching ids for {table}: {e}")
            return []
        finally:
            session.close()
    
    # ========================================
    # GitHubRepository Operations
    # ========================================
    
    def add_github_repository(self, 
                             name: str,
                             url: str,
                             about: Optional[str] = None,
                             created_at: Optional[datetime] = None,
                             last_commit: Optional[datetime] = None,
                             num_stars: int = 0,
                             num_issues: int = 0,
                             num_containers: int = 0,
                             docker_compose_commands: Optional[Dict] = None,
                             readme: Optional[str] = None,
                             docker_compose_filepath: Optional[list] = None,
                             cleaned_docker_compose_filepath: Optional[list] = None) -> Tuple[bool, str, Optional[GitHubRepository]]:
        """
        Add a new GitHub repository to the database.
        
        Returns:
            Tuple[bool, str, Optional[GitHubRepository]]: (success, message, repository_object)
        """
        session = self.get_session()
        try:
            if docker_compose_commands is None:
                docker_compose_commands = {}
            if docker_compose_filepath is None:
                docker_compose_filepath = []
            if cleaned_docker_compose_filepath is None:
                cleaned_docker_compose_filepath = []

            # Check if repository already exists
            existing = session.query(GitHubRepository).filter_by(url=url).first()
            if existing:
                paths = list(existing.docker_compose_filepath or [])
                cleaned_paths = list(existing.cleaned_docker_compose_filepath or [])
                new_paths: list = []
                new_cleaned_paths: list = []
                if docker_compose_filepath:
                    new_paths = [p for p in docker_compose_filepath if p not in paths]
                    if new_paths:
                        paths.extend(new_paths)
                        existing.docker_compose_filepath = paths
                if cleaned_docker_compose_filepath:
                    new_cleaned_paths = [p for p in cleaned_docker_compose_filepath if p not in cleaned_paths]
                    if new_cleaned_paths:
                        cleaned_paths.extend(new_cleaned_paths)
                        existing.cleaned_docker_compose_filepath = cleaned_paths
                if new_paths or (cleaned_docker_compose_filepath and new_cleaned_paths):
                    session.commit()
                    logger.info(f"✅ Added {new_paths or new_cleaned_paths} to repository: {name}")
                    return True, f"Successfully added additional docker compose file(s) to repository {name}", existing
                return False, f"Repository with URL {url} and docker compose path(s) {docker_compose_filepath} already exists", existing
                return False, f"No docker compose filepaths provided for repository {name}", existing
            
            repository = GitHubRepository(
                name=name,
                url=url,
                about=about,
                created_at=created_at,
                last_commit=last_commit,
                num_stars=num_stars,
                num_issues=num_issues,
                num_containers=num_containers,
                docker_compose_commands=docker_compose_commands,
                readme=readme,
                docker_compose_filepath=docker_compose_filepath,
                cleaned_docker_compose_filepath=cleaned_docker_compose_filepath
            )
            
            session.add(repository)
            session.commit()
            session.refresh(repository)
            
            logger.info(f"✅ Added repository: {name}")
            return True, f"Successfully added repository {name}", repository
            
        except IntegrityError as e:
            session.rollback()
            logger.error(f"❌ Integrity error adding repository {name}: {e}")
            return False, f"Integrity error: {str(e)}", None
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"❌ Database error adding repository {name}: {e}")
            return False, f"Database error: {str(e)}", None
        finally:
            session.close()
    
    def update_github_repository(self, 
                                repository_id: int,
                                **kwargs) -> Tuple[bool, str, Optional[GitHubRepository]]:
        """
        Update an existing GitHub repository.
        
        Args:
            repository_id: ID of the repository to update
            **kwargs: Fields to update
            
        Returns:
            Tuple[bool, str, Optional[GitHubRepository]]: (success, message, repository_object)
        """
        session = self.get_session()
        try:
            repository = session.query(GitHubRepository).filter_by(id=repository_id).first()
            if not repository:
                return False, f"Repository with ID {repository_id} not found", None
            
            # Update fields
            for field, value in kwargs.items():
                if hasattr(repository, field):
                    setattr(repository, field, value)
            
            session.commit()
            session.refresh(repository)
            
            # logger.info(f"✅ Updated repository: {repository.name}")
            return True, f"Successfully updated repository {repository.name}", repository
            
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"❌ Database error updating repository {repository_id}: {e}")
            return False, f"Database error: {str(e)}", None
        finally:
            session.close()
    
    def get_github_repository(self, 
                             repository_id: Optional[int] = None,
                             url: Optional[str] = None) -> Optional[GitHubRepository]:
        """Get a GitHub repository by ID or URL."""
        session = self.get_session()
        try:
            if repository_id:
                return session.query(GitHubRepository).filter_by(id=repository_id).first()
            elif url:
                return session.query(GitHubRepository).filter_by(url=url).first()
            else:
                raise ValueError("Either repository_id or url must be provided")
        finally:
            session.close()


    def get_unchecked_repository(self,
                                 stage: float) -> Optional[GitHubRepository]:
        """Get a repository that has not been processed by the stage"""
        session = self.get_session()

        if stage == 3:
            """
            Get repositories without traffic parameters yet.
            """
            try:
                return (
                    session.query(GitHubRepository)
                    .outerjoin(
                        TrafficParameters,
                        TrafficParameters.id == GitHubRepository.id
                    )
                    .filter(
                        TrafficParameters.id.is_(None),
                        func.coalesce(func.cardinality(GitHubRepository.cleaned_docker_compose_filepath), 0) <= 3
                    )
                    .order_by(
                        GitHubRepository.num_stars.desc(),
                    )
                    .first()
                )

            finally:
                session.close()

        if stage == 4:
            '''
            Get unprocessed repositories that passed the one minute check.
            '''
            try:
                return (
                    session.query(TrafficParameters)
                    .filter(
                        TrafficParameters.one_minute_check.is_(True),
                        TrafficParameters.application_flow.is_(None)
                    )
                    .order_by(
                        TrafficParameters.id.asc(),
                    )
                    .first()
                )
            finally:
                session.close()

        session.close()
        return None
        if stage == 5:
            try:
                return (
                    session.query(GitHubRepository)
                    .join(
                        TrafficParameters,
                        TrafficParameters.id == GitHubRepository.id
                    )
                    .filter(
                        TrafficParameters.failure_reason != 'PCAP has not met the size requirements.',
                        ~GitHubRepository.id.in_(select(AgentOutput.id)),
                        GitHubRepository.id > 800,
                        func.cardinality(GitHubRepository.cleaned_docker_compose_filepath) > 0
                    ).order_by(
                        func.cardinality(GitHubRepository.cleaned_docker_compose_filepath).asc(),
                        GitHubRepository.num_stars.desc(),  
                        GitHubRepository.num_issues.asc(),
                    ).first()
                )
            finally:
                session.close()

    def get_unchecked_repositories(
            self,
            stage: float,
            limit: int = 5) -> List[GitHubRepository]:
        """
        Return up to ``limit`` unchecked repositories for the given stage
        Used to coordinate rabbitmq queues
        """
        session = self.get_session()
        try:
            if stage != 3:
                raise ValueError("get_unchecked_repositories currently supports stage 3 only")

            claimable_statuses = ("pending",)
            traffic_params_exists = (
                session.query(TrafficParameters.id)
                .filter(TrafficParameters.id == GitHubRepository.id)
                .exists()
            )

            query = (
                session.query(GitHubRepository)
                .filter(
                    ~traffic_params_exists,
                    or_(
                        GitHubRepository.stage3_processing_status.is_(None),
                        GitHubRepository.stage3_processing_status.in_(claimable_statuses),
                    ),
                )
                .order_by(
                    func.cardinality(GitHubRepository.cleaned_docker_compose_filepath).asc(),
                    GitHubRepository.num_stars.desc(),  
                    GitHubRepository.num_issues.asc(),
                    )
                .with_for_update(skip_locked=True)
                .limit(limit)
            )

            repositories = query.all()
            if not repositories:
                return []

            for repository in repositories:
                repository.stage3_processing_status = "queued"
            session.expire_on_commit = False
            session.commit()

            for repository in repositories:
                session.expunge(repository)
            return repositories
        finally:
            session.close()

    def get_repository_with_build_or_start_failure(
        self,
        run_id: int
    ) -> Optional[GitHubRepository]:
        """
        Fetch the highest-priority repository whose traffic capture failed for image
        build or container start errors and has not yet been processed by agent runs.

        Args:
            run_id: Filter out repositories that already have results for this run.
        """
        session = self.get_session()
        try:
            run_result_query = session.query(AgentRunResult.repository_id).filter(
                AgentRunResult.repository_id == GitHubRepository.id
            )
            run_result_query = run_result_query.filter(AgentRunResult.run_id == run_id)
            run_result_exists = run_result_query.exists()

            failure_filters = [
                TrafficParameters.failure_reason.like('%"status": "error"%'),
                TrafficParameters.failure_reason.like("%Image building failed%"),
                TrafficParameters.failure_reason.like("%service_errors%"),
                TrafficParameters.failure_reason.like("%failed to start containers%"),
                TrafficParameters.failure_reason.like(
                    "%Repo cannot be run with default docker-compose command. %"
                ),
            ]

            repository = (
                session.query(GitHubRepository)
                .join(TrafficParameters, TrafficParameters.id == GitHubRepository.id)
                .filter(
                    or_(*failure_filters),
                    ~run_result_exists,
                )
                # Prefer repositories with more stars, then more recent last commit.
                .order_by(
                    GitHubRepository.num_stars.desc(),
                    GitHubRepository.last_commit.desc().nullslast(),
                )
                .first()
            )
            return repository
        finally:
            session.close()


    # ========================================
    # TrafficParameters Operations
    # ========================================

    def update_traffic_parameters(self,
                                  repository_id: int,
                                  **kwargs) -> Tuple[bool, str, Optional[TrafficParameters]]:
        """
        Create or update traffic parameters for a repository.

        Args:
            repository_id: Repository identifier (also primary key for traffic parameters)
            **kwargs: Column values to set on the TrafficParameters record

        Returns:
            Tuple[bool, str, Optional[TrafficParameters]]: (success flag, message, record)
        """
        if not kwargs:
            return False, "No values provided to update traffic parameters", None

        # Filter to only valid TrafficParameters attributes
        valid_updates: Dict[str, Any] = {}
        invalid_fields: List[str] = []
        for field, value in kwargs.items():
            if field == "id":
                invalid_fields.append(field)
                continue
            if hasattr(TrafficParameters, field):
                valid_updates[field] = value
            else:
                invalid_fields.append(field)

        if not valid_updates:
            if invalid_fields:
                logger.warning(
                    "No valid TrafficParameters fields provided. Invalid fields: "
                    + ", ".join(invalid_fields)
                )
            return False, "No valid fields provided for update"

        session = self.get_session()
        try:
            traffic_params = session.query(TrafficParameters).filter_by(id=repository_id).first()
            created = False

            if not traffic_params:
                repository_exists = session.query(GitHubRepository.id).filter_by(id=repository_id).scalar()
                if not repository_exists:
                    return False, f"Repository with ID {repository_id} not found"
                traffic_params = TrafficParameters(id=repository_id)
                session.add(traffic_params)
                created = True

            for field, value in valid_updates.items():
                setattr(traffic_params, field, value)

            session.commit()
            session.refresh(traffic_params)

            if invalid_fields:
                logger.warning(
                    f"Ignored invalid TrafficParameters fields for repository {repository_id}: "
                    + ", ".join(invalid_fields)
                )

            action = "Created" if created else "Updated"
            logger.info(
                f"✅ {action} traffic parameters for repository ID {repository_id} "
                f"({', '.join(valid_updates.keys())})"
            )
            return True, f"{action} traffic parameters for repository ID {repository_id}"

        except SQLAlchemyError as e:
            session.rollback()
            logger.error(
                f"❌ Database error updating traffic parameters for repository {repository_id}: {e}"
            )
            return False, f"Database error: {str(e)}"
        finally:
            session.close()

    def get_traffic_parameters_by_id(self, repository_id: int) -> Optional[TrafficParameters]:
        """Retrieve the TrafficParameters row for a repository.

        Args:
            repository_id: Repository identifier (TrafficParameters primary key).

        Returns:
            TrafficParameters ORM instance, or None if not found / on error.
        """
        session = self.get_session()
        try:
            record = (
                session.query(TrafficParameters)
                .filter(TrafficParameters.id == repository_id)
                .first()
            )
            if record is not None:
                session.expunge(record)
            return record
        except SQLAlchemyError as e:
            logger.error(
                f"❌ Database error retrieving TrafficParameters for repository {repository_id}: {e}"
            )
            return None
        finally:
            session.close()

    def get_traffic_parameters_pending_llm_analysis(
        self,
    ) -> Optional[TrafficParameters]:
        """
        Retrieve the earliest TrafficParameters row that already has application flow data
        but is missing LLM traffic analysis.

        Returns:
            Single TrafficParameters object awaiting LLM analysis, or None if unavailable.
        """
        session = self.get_session()
        try:
            result = (
                session.query(TrafficParameters)
                .filter(
                    TrafficParameters.application_flow.isnot(None),
                    TrafficParameters.application_traffic_present.is_(None),
                )
                .order_by(TrafficParameters.id.asc())
                .first()
            )
            return result
        finally:
            session.close()

    def get_repository_applications(self) -> Dict[str, int]:
        """
        Retrieve unique repository_id -> application mappings from both
        `agent_traffic_parameters` (prefer most-recent per repository) and
        `traffic_parameters`, then return a mapping of application -> count
        of unique repositories that reported that application.

        Preference order: use the most recent AgentTrafficParameters entry
        for a repository if present, otherwise fall back to TrafficParameters.
        Null/empty application values are excluded.
        Returns:
            Dict[str, int]: {application_string: unique_repository_count, ...}
        """
        session = self.get_session()
        try:
            apps: Dict[int, Optional[str]] = {}

            # Load agent entries ordered by id then newest updated_at first
            try:
                agent_rows = (
                    session.query(
                        AgentTrafficParameters.id,
                        AgentTrafficParameters.application,
                        AgentTrafficParameters.updated_at,
                    )
                    .order_by(AgentTrafficParameters.id.asc(), AgentTrafficParameters.updated_at.desc())
                    .all()
                )
            except SQLAlchemyError:
                agent_rows = []

            seen: set = set()
            for rid, application, _updated in agent_rows:
                if rid in seen:
                    continue
                # Exclude null/empty application values
                if application is None:
                    continue
                app_str = str(application).strip()
                if app_str == "":
                    continue
                seen.add(rid)
                apps[int(rid)] = app_str

            # Fill from TrafficParameters for repository_ids not already present
            try:
                traffic_rows = (
                    session.query(TrafficParameters.id, TrafficParameters.application)
                    .order_by(TrafficParameters.id.asc())
                    .all()
                )
            except SQLAlchemyError:
                traffic_rows = []

            for rid, application in traffic_rows:
                if int(rid) in apps:
                    continue
                # Exclude null/empty application values
                if application is None:
                    continue
                app_str = str(application).strip()
                if app_str == "":
                    continue
                apps[int(rid)] = app_str

            # Split, normalize and deduplicate tokens per repository, then count unique repos per token
            token_map: Dict[str, set] = {}
            for rid, app_str in apps.items():
                # split on common delimiters (comma, pipe, semicolon)
                tokens = re.split(r"[,\|;]+", app_str)
                seen_tokens = set()
                for t in tokens:
                    if t is None:
                        continue
                    norm = str(t).strip().lower()
                    if norm == "":
                        continue
                    # avoid counting same token twice for one repo
                    if norm in seen_tokens:
                        continue
                    seen_tokens.add(norm)
                    token_map.setdefault(norm, set()).add(int(rid))

            counts: Dict[str, int] = {k: len(v) for k, v in token_map.items()}
            total_repositories = len(apps)

            return {
                "total_repositories": total_repositories,
                "applications": counts,
            }
        except SQLAlchemyError as e:
            logger.error(f"❌ Database error retrieving repository applications: {e}")
            return {}
        finally:
            session.close()

    def get_distinct_applications(self) -> List[str]:
        """
        Retrieve all distinct application types from both `traffic_parameters`
        and `agent_traffic_parameters` tables.

        This will:
        - Query the `application` column from both tables
        - Ignore null/empty values
        - Split on common delimiters (comma, pipe, semicolon)
        - Normalize tokens by stripping whitespace and lower-casing
        - Return a sorted list of unique application strings

        Returns:
            List[str]: Sorted list of distinct application tokens (lowercased)
        """
        session = self.get_session()
        try:
            values = []

            try:
                tp_rows = session.query(TrafficParameters.application).all()
            except SQLAlchemyError:
                tp_rows = []

            try:
                atp_rows = session.query(AgentTrafficParameters.application).all()
            except SQLAlchemyError:
                atp_rows = []

            for (val,) in tp_rows + atp_rows:
                if val is None:
                    continue
                s = str(val).strip()
                if s == "":
                    continue
                values.append(s)

            tokens_set = set()
            for v in values:
                parts = re.split(r"[,\|;]+", v)
                for p in parts:
                    if p is None:
                        continue
                    t = str(p).strip().lower()
                    if t == "":
                        continue
                    tokens_set.add(t)

            return sorted(tokens_set)
        except SQLAlchemyError as e:
            logger.error(f"❌ Database error retrieving distinct applications: {e}")
            return []
        finally:
            session.close()

    def get_traffic_parameters_pending_label(self) -> Optional[GitHubRepository]:
        """
        Retrieve a repository whose traffic analysis indicates application traffic is present
        and therefore needs labeling.

        Currently selects the earliest repository where TrafficParameters.application_traffic_present
        is truthy (case-insensitive match on the string 'true').

        This is a test function to check the output of labels.

        Returns:
            Optional[GitHubRepository]: Repository awaiting labeling, or None if none found.
        """
        session = self.get_session()
        try:
            repository = (
                session.query(GitHubRepository)
                .join(TrafficParameters, TrafficParameters.id == GitHubRepository.id)
                .filter(
                    func.lower(TrafficParameters.application_traffic_present) == "true",
                    ~GitHubRepository.application_labels.any(),
                )
                .order_by(GitHubRepository.id.asc())
                .first()
            )
            return repository
        finally:
            session.close()

    def get_unchecked_traffic_parameters(self) -> Optional[Any]:
        """
        Retrieve the earliest traffic-parameter row that passed the one-minute check
        but has not yet been processed into an application_flow.

        Returns a lightweight row containing the fields needed by stage 4:
        - id, name (from GitHubRepository)
        - one_minute_check, application_flow (from TrafficParameters)

        This avoids selecting all columns from TrafficParameters (which can fail if the
        live DB schema is missing newly-added columns) and gives callers access to the repo
        name without an extra query.
        """
        session = self.get_session()
        try:
            return (
                session.query(
                    GitHubRepository.id.label("id"),
                    GitHubRepository.name.label("name"),
                    TrafficParameters.one_minute_check.label("one_minute_check"),
                    TrafficParameters.application_flow.label("application_flow"),
                )
                .join(TrafficParameters, TrafficParameters.id == GitHubRepository.id)
                .filter(
                    TrafficParameters.one_minute_check.is_(True),
                    TrafficParameters.application_flow.is_(None),
                )
                .order_by(GitHubRepository.id.asc())
                .first()
            )
        finally:
            session.close()

    # ========================================
    # AgentTrafficParameters Operations
    # ========================================

    def get_unchecked_agent_traffic_parameters(self) -> Optional[Any]:
        """
        Retrieve the earliest agent traffic-parameter row that passed the one-minute check
        but has not yet been processed into an application_flow.

        Returns a lightweight row containing the fields needed by stage 4:
        - id, name (from GitHubRepository)
        - processing_host, model, run_id (from AgentTrafficParameters)

        This avoids selecting all columns from AgentTrafficParameters (which can fail if the
        live DB schema is missing newly-added columns) and gives callers access to the repo
        name without an extra query.
        """
        session = self.get_session()
        try:
            return (
                session.query(
                    GitHubRepository.id.label("id"),
                    GitHubRepository.name.label("name"),
                    AgentTrafficParameters.processing_host.label("processing_host"),
                    AgentTrafficParameters.model.label("model"),
                    AgentTrafficParameters.run_id.label("run_id"),
                )
                .join(AgentTrafficParameters, AgentTrafficParameters.id == GitHubRepository.id)
                .filter(
                    AgentTrafficParameters.one_minute_check.is_(True),
                    AgentTrafficParameters.application_flow.is_(None),
                )
                .order_by(AgentTrafficParameters.id.asc())
                .first()
            )
        finally:
            session.close()

    def get_agent_traffic_parameters_by_id(
        self,
        repository_id: int,
        model: Optional[str] = None,
        run_id: Optional[int] = None,
    ) -> Optional[AgentTrafficParameters]:
        """Retrieve an AgentTrafficParameters row for a repository.

        Note: `agent_traffic_parameters` uses a composite primary key
        `(id, model, run_id)`. If `model` and `run_id` are not provided, this
        returns the most recently updated row for the repository.

        Args:
            repository_id: Repository identifier (AgentTrafficParameters.id).
            model: Optional model identifier to disambiguate a specific agent row.
            run_id: Optional run identifier to disambiguate a specific agent row.

        Returns:
            AgentTrafficParameters ORM instance, or None if not found / on error.
        """
        session = self.get_session()
        try:
            query = session.query(AgentTrafficParameters).filter(
                AgentTrafficParameters.id == repository_id
            )
            if model is not None:
                query = query.filter(AgentTrafficParameters.model == model)
            if run_id is not None:
                query = query.filter(AgentTrafficParameters.run_id == run_id)

            record = (
                query.order_by(
                    AgentTrafficParameters.updated_at.desc().nullslast(),
                    AgentTrafficParameters.run_id.desc(),
                    AgentTrafficParameters.model.asc(),
                )
                .first()
            )
            if record is not None:
                session.expunge(record)
            return record
        except SQLAlchemyError as e:
            logger.error(
                f"❌ Database error retrieving AgentTrafficParameters for repository {repository_id}: {e}"
            )
            return None
        finally:
            session.close()

    def get_agent_application_flow(
        self, repository_id: int
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch the agent-generated application_flow blob for a repository.

        Args:
            repository_id: Repository identifier tied to AgentTrafficParameters.id

        Returns:
            Parsed JSON/Dict stored in application_flow, or None if missing
            or repository has no agent traffic parameters yet.
        """
        session = self.get_session()
        try:
            result = (
                session.query(AgentTrafficParameters.application_flow)
                .filter(AgentTrafficParameters.id == repository_id)
                .scalar()
            )
            return result
        except SQLAlchemyError as e:
            logger.error(
                f"❌ Database error retrieving agent application_flow for repository {repository_id}: {e}"
            )
            return None
        finally:
            session.close()

    def update_agent_traffic_parameters(self,
                                        repository_id: int,
                                        run_id: int,
                                        **kwargs) -> Tuple[bool, str]:
        """
        Create or update agent-generated traffic parameters for a repository.

        Args:
            repository_id: Repository identifier (also primary key for agent traffic parameters)
            run_id: Run identifier for this agent traffic record
            **kwargs: Column values to set on the AgentTrafficParameters record

        Returns:
            Tuple[bool, str]: (success flag, message)
        """
        if not kwargs:
            return False, "No values provided to update agent traffic parameters"

        # Always anchor updates on the composite key (repository_id, model, run_id)
        model_value = kwargs.get("model")
        if model_value is None or str(model_value).strip() == "":
            model_value = "default"
        else:
            model_value = str(model_value)

        valid_updates: Dict[str, Any] = {"run_id": run_id, "model": model_value}
        invalid_fields: List[str] = []
        for field, value in kwargs.items():
            if field in {"id", "run_id"}:
                invalid_fields.append(field)
                continue
            if hasattr(AgentTrafficParameters, field):
                valid_updates[field] = value
            else:
                invalid_fields.append(field)

        if not valid_updates:
            if invalid_fields:
                logger.warning(
                    "No valid AgentTrafficParameters fields provided. Invalid fields: "
                    + ", ".join(invalid_fields)
                )
            return False, "No valid fields provided for update"

        session = self.get_session()
        try:
            records = (
                session.query(AgentTrafficParameters)
                .filter_by(id=repository_id, model=model_value, run_id=run_id)
                .order_by(AgentTrafficParameters.updated_at.desc().nullslast())
                .all()
            )
            agent_params = records[0] if records else None
            created = False

            if not agent_params:
                repository_exists = (
                    session.query(GitHubRepository.id)
                    .filter_by(id=repository_id)
                    .scalar()
                )
                if not repository_exists:
                    return False, f"Repository with ID {repository_id} not found"
                agent_params = AgentTrafficParameters(
                    id=repository_id, model=model_value, run_id=run_id
                )
                session.add(agent_params)
                created = True

            for field, value in valid_updates.items():
                setattr(agent_params, field, value)

            session.commit()
            session.refresh(agent_params)

            if invalid_fields:
                logger.warning(
                    f"Ignored invalid AgentTrafficParameters fields for repository {repository_id}: "
                    + ", ".join(invalid_fields)
                )

            action = "Created" if created else "Updated"
            logger.info(
                f"✅ {action} agent traffic parameters for repository ID {repository_id} "
                f"({', '.join(valid_updates.keys())})"
            )
            return True, f"{action} agent traffic parameters for repository ID {repository_id}"

        except SQLAlchemyError as e:
            session.rollback()
            logger.error(
                f"❌ Database error updating agent traffic parameters for repository {repository_id}: {e}"
            )
            return False, f"Database error: {str(e)}"
        finally:
            session.close()

    # ========================================
    # AgentRunResult Operations
    # ========================================

    def record_agent_run_result(
        self,
        repository_id: int,
        run_id: int,
        model: str,
        env_result: Optional[Dict[str, Any]],
        codex_result: Optional[Dict[str, Any]],
        codex_stdout: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[AgentRunResult]]:
        """
        Persist a single run_agents invocation result for the given repository and model.
        """

        def _coerce_float(value: Any) -> Optional[float]:
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def _coerce_steps(value: Any) -> Optional[List[str]]:
            if value is None:
                return None
            if isinstance(value, list):
                return [str(step) for step in value]
            if isinstance(value, str):
                return [value]
            return None

        def _coerce_int(name: str, value: Any) -> int:
            if isinstance(value, bool):
                raise ValueError(f"{name} must be an int, got bool {value!r}")
            if isinstance(value, str):
                value = value.strip()
            try:
                return int(value)
            except (TypeError, ValueError):
                try:
                    as_float = float(value)
                except (TypeError, ValueError) as e:
                    raise ValueError(f"{name} must be an int, got {value!r}") from e
                if not as_float.is_integer():
                    raise ValueError(f"{name} must be an int, got non-integer {value!r}")
                return int(as_float)

        def _format_pk(rid: Any, m: Any, r: Any) -> str:
            return (
                f"({rid!r}:{type(rid).__name__}, "
                f"{m!r}:{type(m).__name__}, "
                f"{r!r}:{type(r).__name__})"
            )

        ENV_EXPECTED_KEYS = {"status", "environmental_variables_added", "env_location"}
        CODEX_EXPECTED_KEYS = {"working", "steps_taken"}

        try:
            repository_id = _coerce_int("repository_id", repository_id)
            run_id = _coerce_int("run_id", run_id)
        except ValueError as e:
            return False, str(e), None

        session = self.get_session()
        try:
            repository_exists = (
                session.query(GitHubRepository.id)
                .filter_by(id=repository_id)
                .scalar()
            )
            if not repository_exists:
                return False, f"Repository with ID {repository_id} not found", None

            env_messages = env_result.get("messages") if isinstance(env_result, dict) else []
            env_last_message = env_messages[-1] if env_messages else None
            env_content = getattr(env_last_message, "content", env_last_message)
            env_dict = _normalize_result(env_content, ENV_EXPECTED_KEYS)
            if isinstance(env_messages, list):
                env_result["messages"] = [str(msg) for msg in env_messages]

            codex_messages = codex_result.get("messages") if isinstance(codex_result, dict) else []
            codex_last_message = codex_messages[-1] if codex_messages else None
            codex_content = getattr(codex_last_message, "content", codex_last_message)
            codex_dict = _normalize_result(codex_content, CODEX_EXPECTED_KEYS)
            if (
                not isinstance(codex_dict, dict)
                or codex_dict.get("status") is False
            ):
                if isinstance(codex_result, dict) and CODEX_EXPECTED_KEYS.issubset(codex_result.keys()):
                    codex_dict = {key: codex_result.get(key) for key in CODEX_EXPECTED_KEYS}
                else:
                    stdout_dict = _normalize_result(codex_stdout or "", CODEX_EXPECTED_KEYS)
                    if isinstance(stdout_dict, dict) and stdout_dict.get("status") is not False:
                        codex_dict = stdout_dict
                    else:
                        codex_dict = {
                            "working": codex_result.get("working") if isinstance(codex_result, dict) else None,
                            "steps_taken": codex_result.get("steps_taken") if isinstance(codex_result, dict) else None,
                        }
            if isinstance(codex_messages, list):
                codex_result["messages"] = [str(msg) for msg in codex_messages]

            traffic_types = None
            env_vars_payload = None
            env_locations_payload = None
            if isinstance(env_result, dict):
                traffic_types = env_result.get("traffic_types")
                env_vars_payload = env_result.get("env_vars")
                env_locations_payload = env_result.get("env_locations")
            if traffic_types is not None:
                notes = json.dumps(traffic_types, ensure_ascii=True)

            primary_key = (repository_id, model, run_id)
            run_result = session.get(AgentRunResult, primary_key)
            created = False

            if run_result:
                if (
                    run_result.repository_id != repository_id
                    or run_result.model != model
                    or run_result.run_id != run_id
                ):
                    logger.error(
                        "Primary key mismatch when updating agent run result: "
                        f"existing {_format_pk(run_result.repository_id, run_result.model, run_result.run_id)} "
                        f"!= provided {_format_pk(repository_id, model, run_id)}"
                    )
                    return (
                        False,
                        "Primary key mismatch when updating agent run result",
                        None,
                    )
            else:
                run_result = AgentRunResult(
                    repository_id=repository_id,
                    model=model,
                    run_id=run_id,
                )
                session.add(run_result)
                created = True

            run_result.env_status = env_dict.get("status")
            run_result.env_environmental_variables = (
                env_vars_payload
                if env_vars_payload is not None
                else env_dict.get("environmental_variables_added")
            )
            run_result.env_location = (
                env_locations_payload
                if env_locations_payload is not None
                else env_dict.get("env_location")
            )
            run_result.env_latency_seconds = _coerce_float(env_result.get("latency_seconds"))
            run_result.codex_working = codex_dict.get("working")
            run_result.codex_steps_taken = _coerce_steps(codex_dict.get("steps_taken"))
            run_result.codex_latency_seconds = _coerce_float(
                codex_result.get("latency_seconds") if isinstance(codex_result, dict) else None
            )
            run_result.raw_env_result = env_result
            run_result.raw_codex_result = {
                "parsed": codex_result,
                "stdout": codex_stdout,
            }
            run_result.notes = notes

            session.commit()
            session.refresh(run_result)

            logger.info(
                f"✅ {'Created' if created else 'Updated'} agent run result for repository ID {repository_id} "
                f"model '{model}' run_id {run_id} (env_status={run_result.env_status}, "
                f"codex_working={run_result.codex_working})"
            )
            return True, "Successfully recorded agent run result", run_result

        except SQLAlchemyError as e:
            session.rollback()
            logger.error(
                f"❌ Database error recording agent run result for repository {repository_id}: {e}"
            )
            return False, f"Database error: {str(e)}", None
        finally:
            session.close()

    def get_agent_run_result(
        self, repository_id: int, model: str, run_id: int
    ) -> Optional[AgentRunResult]:
        """
        Retrieve a single agent run result by repository, model, and run_id.
        """
        session = self.get_session()
        try:
            return session.get(AgentRunResult, (repository_id, model, run_id))
        except SQLAlchemyError as e:
            logger.error(
                f"❌ Database error retrieving agent run result for repository {repository_id} "
                f"model '{model}' run_id {run_id}: {e}"
            )
            return None
        finally:
            session.close()

    def get_repo_for_gpt5mini(self, run_id: int) -> Optional[GitHubRepository]:
        """
        Fetch a repository whose qwen3-coder codex run failed and that does not
        yet have a gpt-5-mini run recorded for the provided run_id.
        """
        session = self.get_session()
        try:
            gpt5_alias = aliased(AgentRunResult)
            gpt5_exists = (
                session.query(gpt5_alias.repository_id)
                .filter(
                    gpt5_alias.repository_id == AgentRunResult.repository_id,
                    gpt5_alias.model == "gpt-5-mini",
                    gpt5_alias.run_id == run_id,
                )
                .exists()
            )

            repository = (
                session.query(GitHubRepository)
                .join(AgentRunResult, AgentRunResult.repository_id == GitHubRepository.id)
                .filter(
                    AgentRunResult.model == "qwen3-coder:480b",
                    AgentRunResult.codex_working.is_(False),
                    AgentRunResult.run_id == run_id,
                    ~gpt5_exists,
                )
                .order_by(AgentRunResult.updated_at.desc())
                .first()
            )

            if repository:
                session.expunge(repository)
            return repository
        finally:
            session.close()

    # ========================================
    # ApplicationLabel Operations
    # ========================================

    def add_application_label(self, 
                             name: str,
                             description: Optional[str] = None) -> Tuple[bool, str, Optional[ApplicationLabel]]:
        """
        Add a new application label.
        
        Returns:
            Tuple[bool, str, Optional[ApplicationLabel]]: (success, message, label_object)
        """
        session = self.get_session()
        try:
            # Check if label already exists
            existing = session.query(ApplicationLabel).filter_by(name=name).first()
            if existing:
                return False, f"Application label '{name}' already exists", existing
            
            label = ApplicationLabel(
                name=name,
                description=description
            )
            
            session.add(label)
            session.commit()
            session.refresh(label)
            
            logger.info(f"✅ Added application label: {name}")
            return True, f"Successfully added application label '{name}'", label
            
        except IntegrityError as e:
            session.rollback()
            logger.error(f"❌ Integrity error adding application label {name}: {e}")
            return False, f"Integrity error: {str(e)}", None
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"❌ Database error adding application label {name}: {e}")
            return False, f"Database error: {str(e)}", None
        finally:
            session.close()
    
    def update_application_label(self, 
                                label_id: int,
                                name: Optional[str] = None,
                                description: Optional[str] = None) -> Tuple[bool, str, Optional[ApplicationLabel]]:
        """Update an existing application label."""
        session = self.get_session()
        try:
            label = session.query(ApplicationLabel).filter_by(id=label_id).first()
            if not label:
                return False, f"Application label with ID {label_id} not found", None
            
            if name is not None:
                label.name = name
            if description is not None:
                label.description = description
            
            session.commit()
            session.refresh(label)
            
            logger.info(f"✅ Updated application label: {label.name}")
            return True, f"Successfully updated application label '{label.name}'", label
            
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"❌ Database error updating application label {label_id}: {e}")
            return False, f"Database error: {str(e)}", None
        finally:
            session.close()
    
    def get_application_label(self, 
                             label_id: Optional[int] = None,
                             name: Optional[str] = None) -> Optional[ApplicationLabel]:
        """Get an application label by ID or name."""
        session = self.get_session()
        try:
            if label_id:
                return session.query(ApplicationLabel).filter_by(id=label_id).first()
            elif name:
                return session.query(ApplicationLabel).filter_by(name=name).first()
            else:
                raise ValueError("Either label_id or name must be provided")
        finally:
            session.close()
    
    # ========================================
    # Relationship Operations
    # ========================================
    
    def assign_application_label_to_repository(self, 
                                              repository_id: int,
                                              label_id: int,
                                              confidence: float) -> Tuple[bool, str]:
        """Assign an application label to a repository."""
        session = self.get_session()
        try:
            repository = session.query(GitHubRepository).filter_by(id=repository_id).first()
            label = session.query(ApplicationLabel).filter_by(id=label_id).first()
            
            if not repository:
                return False, f"Repository with ID {repository_id} not found"
            if not label:
                return False, f"Application label with ID {label_id} not found"
            
            if label in repository.application_labels:
                return False, f"Label '{label.name}' already assigned to repository '{repository.name}'"
            
            repository.application_labels.append(label)
            session.commit()
            
            logger.info(f"✅ Assigned label '{label.name}' to repository '{repository.name}'")
            return True, f"Successfully assigned label '{label.name}' to repository '{repository.name}'"
            
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"❌ Database error assigning label to repository: {e}")
            return False, f"Database error: {str(e)}"
        finally:
            session.close()
    
    def remove_application_label_from_repository(self, 
                                                repository_id: int,
                                                label_id: int) -> Tuple[bool, str]:
        """Remove an application label from a repository."""
        session = self.get_session()
        try:
            repository = session.query(GitHubRepository).filter_by(id=repository_id).first()
            label = session.query(ApplicationLabel).filter_by(id=label_id).first()
            
            if not repository:
                return False, f"Repository with ID {repository_id} not found"
            if not label:
                return False, f"Application label with ID {label_id} not found"
            
            if label not in repository.application_labels:
                return False, f"Label '{label.name}' not assigned to repository '{repository.name}'"
            
            repository.application_labels.remove(label)
            session.commit()
            
            logger.info(f"✅ Removed label '{label.name}' from repository '{repository.name}'")
            return True, f"Successfully removed label '{label.name}' from repository '{repository.name}'"
            
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"❌ Database error removing label from repository: {e}")
            return False, f"Database error: {str(e)}"
        finally:
            session.close()

    def get_repository_without_application_labels(self) -> Optional[GitHubRepository]:
        """
        Retrieve the earliest GitHubRepository row that has no entries in
        repository_application_labels.

        Returns:
            Optional[GitHubRepository]: Repository with no application labels, or None if all are labeled.
        """
        session = self.get_session()
        try:
            repository = (
                session.query(GitHubRepository)
                .outerjoin(
                    repository_application_labels,
                    GitHubRepository.id == repository_application_labels.c.repository_id,
                )
                .filter(repository_application_labels.c.repository_id.is_(None))
                .order_by(GitHubRepository.id.asc())
                .first()
            )
            return repository
        finally:
            session.close()    

# Convenience instance
db_helper = DatabaseHelper()


# Example usage and testing
if __name__ == "__main__":
    # Add a repository
    success, msg, repo = db_helper.add_github_repository(
        name="docker/compose",
        url="https://github.com/docker/compose",
        about="Multi-container Docker applications",
        num_stars=32000
    )

    # Add a Docker image
    success, msg, image = db_helper.add_docker_image(
        name="postgres", 
        version="14.2",
        description="PostgreSQL database"
    )

    # Add an application label
    success, msg, label = db_helper.add_application_label(
        name="database",
        description="Database applications"
    )

    # Assign relationships
    db_helper.assign_docker_image_to_repository(repo.id, image.id)
    db_helper.assign_application_label_to_repository(repo.id, label.id)

    # Update repository
    db_helper.update_github_repository(
        repo.id,
        num_stars=32500,
    useful_traffic=True
)
