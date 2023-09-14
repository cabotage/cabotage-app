import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy_utils import JSONType, generic_relationship

from sqlalchemy_continuum.plugins.base import Plugin
from sqlalchemy_continuum.factory import ModelFactory
from sqlalchemy_continuum.utils import version_class, version_obj


class ActivityBase(object):
    id = sa.Column(
        sa.BigInteger,
        sa.schema.Sequence("activity_id_seq"),
        primary_key=True,
        autoincrement=True,
    )

    verb = sa.Column(sa.Unicode(255))

    @hybrid_property
    def actor(self):
        return self.transaction.user


class ActivityFactory(ModelFactory):
    model_name = "Activity"

    def create_class(self, manager):
        """
        Create Activity class.
        """

        class Activity(manager.declarative_base, ActivityBase):
            __tablename__ = "activity"
            manager = self

            transaction_id = sa.Column(sa.BigInteger, index=True, nullable=False)

            data = sa.Column(JSONType)

            object_type = sa.Column(sa.String(255))

            object_id = sa.Column(postgresql.UUID(as_uuid=True))

            object_tx_id = sa.Column(sa.BigInteger)

            target_type = sa.Column(sa.String(255))

            target_id = sa.Column(postgresql.UUID(as_uuid=True))

            target_tx_id = sa.Column(sa.BigInteger)

            def _calculate_tx_id(self, obj):
                session = sa.orm.object_session(self)
                if obj:
                    object_version = version_obj(session, obj)
                    if object_version:
                        return object_version.transaction_id

                    version_cls = version_class(obj.__class__)
                    return (
                        session.query(sa.func.max(version_cls.transaction_id))
                        .filter(version_cls.id == obj.id)
                        .scalar()
                    )

            def calculate_object_tx_id(self):
                self.object_tx_id = self._calculate_tx_id(self.object)

            def calculate_target_tx_id(self):
                self.target_tx_id = self._calculate_tx_id(self.target)

            object = generic_relationship(object_type, object_id)

            @hybrid_property
            def object_version_type(self):
                return self.object_type + "Version"

            @object_version_type.expression
            def object_version_type(cls):
                return sa.func.concat(cls.object_type, "Version")

            object_version = generic_relationship(
                object_version_type, (object_id, object_tx_id)
            )

            target = generic_relationship(target_type, target_id)

            @hybrid_property
            def target_version_type(self):
                return self.target_type + "Version"

            @target_version_type.expression
            def target_version_type(cls):
                return sa.func.concat(cls.target_type, "Version")

            target_version = generic_relationship(
                target_version_type, (target_id, target_tx_id)
            )

        Activity.transaction = sa.orm.relationship(
            manager.transaction_cls,
            backref=sa.orm.backref(
                "activities",
            ),
            primaryjoin=(
                "%s.id == Activity.transaction_id" % manager.transaction_cls.__name__
            ),
            foreign_keys=[Activity.transaction_id],
        )
        return Activity


class ActivityPlugin(Plugin):
    def after_build_models(self, manager):
        self.activity_cls = ActivityFactory()(manager)
        manager.activity_cls = self.activity_cls

    def is_session_modified(self, session):
        """
        Return that the session has been modified if the session contains an
        activity class.

        :param session: SQLAlchemy session object
        """
        return any(isinstance(obj, self.activity_cls) for obj in session)

    def before_flush(self, uow, session):
        for obj in session:
            if isinstance(obj, self.activity_cls):
                obj.transaction = uow.current_transaction
                obj.calculate_target_tx_id()
                obj.calculate_object_tx_id()

    def after_version_class_built(self, parent_cls, version_cls):
        pass
