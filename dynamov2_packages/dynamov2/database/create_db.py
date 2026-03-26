from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import os
import sys
import argparse
from typing import Optional
from dotenv import load_dotenv
from .models import Base, GitHubRepository, ApplicationLabel, TrafficParameters

# Load environment variables from .env file
load_dotenv()

current_dir = os.path.abspath(os.path.dirname(__file__))
src_dir = os.path.abspath(os.path.join(current_dir, '..', '..'))
# set sys_path to also look for libs elsewhere
sys.path.append(src_dir)
from ..logger.logger import CustomLogger



# Fetch from environment
POSTGRES_USER = os.getenv("POSTGRES_USER")
if not POSTGRES_USER:
    raise ValueError("POSTGRES_USER environment variable is required")

POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
if not POSTGRES_PASSWORD:
    raise ValueError("POSTGRES_PASSWORD environment variable is required")

POSTGRES_DB = os.getenv("POSTGRES_DB")
if not POSTGRES_DB:
    raise ValueError("POSTGRES_DB environment variable is required")

POSTGRES_CONTAINER = os.getenv("POSTGRES_CONTAINER")
if not POSTGRES_CONTAINER:
    raise ValueError("POSTGRES_CONTAINER environment variable is required")

POSTGRES_PORT = os.getenv("POSTGRES_PORT")
if not POSTGRES_PORT:
    raise ValueError("POSTGRES_PORT environment variable is required")

# Replace with your actual database URL
DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_CONTAINER}:{POSTGRES_PORT}/{POSTGRES_DB}"

logger = CustomLogger("INITIALIZE_DB")

def create_db(force_recreate: bool = False) -> tuple[bool, Optional[list | str]]:
    """
    This function is to (re)create a database with all required tables and relationships!
    
    Creates all tables defined in ``models.py`` (Base.metadata).
    Creates the following tables:
    - github_repositories: Main repository data
    - application_labels: Labels for categorizing applications  
    - repository_application_labels: Many-to-many linking repos to app labels
    
    Args:
        force_recreate: If True, drops existing tables and recreates them
        
    Returns:
        tuple: (success: bool, existing_tables_or_error: list|str|None)
    """
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    try:
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()
        
        # Expected tables from our models
        expected_tables = {
            'github_repositories',
            'application_labels', 
            'repository_application_labels',
            'traffic_parameters'
        }
        # Expected tables from our models (source of truth)
        expected_tables = set(Base.metadata.tables.keys())
        
        if existing_tables:
            logger.info(f"Found {len(existing_tables)} existing tables: {existing_tables}")
            
            if force_recreate:
                logger.warning("⚠️  FORCE RECREATE MODE - This will destroy all existing data!")
                
                # Drop views first if they exist
                try:
                    views_query = text("""
                        SELECT schemaname, viewname
                        FROM pg_views
                        WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
                    """)
                    views_to_drop = session.execute(views_query).fetchall()
                    
                    if views_to_drop:
                        logger.info(f"Found {len(views_to_drop)} view(s) to drop.")
                        for schema, view_name in views_to_drop:
                            full_view_name = f'"{schema}"."{view_name}"'
                            logger.info(f"Dropping view: {full_view_name}")
                            session.execute(text(f"DROP VIEW {full_view_name} CASCADE;"))
                        session.commit()
                        logger.info("✅ Successfully dropped all views.")
                    
                except Exception as e:
                    logger.error(f"❌ Error dropping views: {e}")
                    session.rollback()
                    return (False, f"Failed to drop views: {str(e)}")
                
                # Drop all tables
                logger.info("🗑️  Dropping all existing tables...")
                Base.metadata.drop_all(engine)
                logger.info("✅ All tables dropped successfully.")
                
                # Create all tables
                logger.info("🏗️  Creating all tables from scratch...")
                Base.metadata.create_all(engine)
                
                # Verify tables were created
                updated_inspector = inspect(engine)
                new_tables = updated_inspector.get_table_names()
                missing_tables = expected_tables - set(new_tables)
                
                if missing_tables:
                    logger.error(f"❌ Failed to create tables: {missing_tables}")
                    return (False, f"Missing tables: {missing_tables}")
                
                logger.info(f"✅ Successfully created {len(new_tables)} tables: {sorted(new_tables)}")
                return (True, existing_tables)
                
            else:
                # Check if we have all expected tables
                missing_tables = expected_tables - set(existing_tables)
                extra_tables = set(existing_tables) - expected_tables
                
                if missing_tables:
                    logger.warning(f"⚠️  Missing expected tables: {missing_tables}")
                    logger.info("💡 Run with force_recreate=True to recreate the schema")
                    
                if extra_tables:
                    logger.info(f"ℹ️  Found additional tables: {extra_tables}")
                
                logger.warning("Database exists. Use force_recreate=True to recreate.")
                return (False, existing_tables)
        else:
            # No existing tables - create everything
            logger.info("📋 No existing tables found. Creating complete schema...")
            Base.metadata.create_all(engine)
            
            # Verify creation
            updated_inspector = inspect(engine)
            new_tables = updated_inspector.get_table_names()
            missing_tables = expected_tables - set(new_tables)
            
            if missing_tables:
                logger.error(f"❌ Failed to create tables: {missing_tables}")
                return (False, f"Missing tables: {missing_tables}")
            
            logger.info(f"✅ Successfully created {len(new_tables)} tables: {sorted(new_tables)}")
            logger.info("🎉 Database schema created successfully!")
            return (True, None)
            
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}", exc_info=True)
        session.rollback()
        return (False, str(e))
    finally:
        session.close()
  
if __name__ == "__main__":
    # Set up command line argument parsing
    parser = argparse.ArgumentParser(
        description="Initialize the GitHub repository collector database with all required tables and relationships.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python create_db.py                     # Create database if it doesn't exist
  python create_db.py --force             # Force recreate database (destroys existing data)
  python create_db.py --dry-run           # Check database status without making changes
        """
    )
    
    parser.add_argument(
        '--force', '-f',
        action='store_true',
        default=False,
        help='Force recreate the database, dropping all existing tables and data (default: False)'
    )
    
    parser.add_argument(
        '--dry-run', '-d',
        action='store_true',
        default=False,
        help='Check database status without making any changes (default: False)'
    )
    
    args = parser.parse_args()
    
    # Initialize logger
    logger = CustomLogger("INIT_DB")
    logger.info("🚀 Starting database initialization...")
    
    if args.dry_run:
        logger.info("🔍 DRY RUN MODE - No changes will be made")
        try:
            engine = create_engine(DATABASE_URL)
            inspector = inspect(engine)
            existing_tables = inspector.get_table_names()
            
            expected_tables = {
                'github_repositories',
                'application_labels', 
                'repository_application_labels',
                'traffic_parameters'
            }
            
            if existing_tables:
                logger.info(f"📋 Found {len(existing_tables)} existing tables: {sorted(existing_tables)}")
                missing_tables = expected_tables - set(existing_tables)
                extra_tables = set(existing_tables) - expected_tables
                
                if missing_tables:
                    logger.warning(f"⚠️  Missing expected tables: {missing_tables}")
                if extra_tables:
                    logger.info(f"ℹ️  Extra tables found: {extra_tables}")
                if not missing_tables:
                    logger.info("✅ All expected tables exist")
            else:
                logger.info("📋 No tables found - database is empty")
                
        except Exception as e:
            logger.error(f"❌ Error connecting to database: {e}")
        sys.exit(0)
    
    if args.force:
        logger.warning("⚠️  FORCE MODE - This will destroy all existing data!")
        response = input("Are you sure you want to continue? (yes/no): ")
        if response.lower() not in ['yes', 'y']:
            logger.info("❌ Operation cancelled by user")
            sys.exit(0)
    
    # Run database creation
    success, result = create_db(force_recreate=args.force)
    
    if success:
        if result:  # Had existing tables that were recreated
            logger.info("✅ Database recreated successfully!")
            logger.info(f"🗑️  Replaced {len(result)} existing tables")
        else:  # Created fresh database
            logger.info("✅ Database created successfully!")
        logger.info("🎉 Database is ready for use!")
    else:
        if isinstance(result, list):
            logger.warning("⚠️  Database initialization halted - database already exists")
            logger.info(f"📋 Existing tables: {sorted(result)}")
            logger.info("💡 Use --force to recreate or --dry-run to check status")
        elif isinstance(result, str):
            logger.error(f"❌ Database initialization failed: {result}")
        else:
            logger.error("❌ Unknown error during database initialization")
