from django.db import models
from django.db.models.deletion import CASCADE, DO_NOTHING
from pymysql import NULL
from jwtauth.models import AccountableUser
from django.core.validators import MinValueValidator
import uuid
from entropy.cryptocurrency.models import Cryptocurrency
from entropy.binance_api.master_account import MasterAccountAPI
from datetime import date
from django_q.tasks import async_task


# Create your models here.
class SubAccountDetails(models.Model):
    # email = models.ForeignKey(AccountableUser, to_field="email", on_delete=DO_NOTHING)
    account_id = models.ForeignKey(AccountableUser, on_delete=CASCADE)
    # sub-acc id is number but its too big to store in integer type
    sub_account_id = models.TextField()
    sub_account_email = models.EmailField(unique=True)
    sub_account_api_key = models.TextField()
    sub_account_secret = models.TextField()
    can_trade = models.BooleanField(default=False)
    margin_trade = models.BooleanField(default=False)
    futures_trade = models.BooleanField(default=False)
    is_active = models.BooleanField(default=False)
    is_uds_running = models.BooleanField(default=False)

    class Meta:
        db_table = "sub_account_details"

    def update_uds_status(self, status):
        self.is_uds_running = status
        self.save()

    @classmethod
    def get_or_create(cls, user_id):
        # Get sub-account details
        try:
            user_sub_account = UserSubAccount.objects.filter(user_id_id=user_id).last()
            sub_account = SubAccountDetails.objects.get(
                id=user_sub_account.sub_account_detail_id_id,
                is_active=True
            )
            # return sub_account
        except Exception as e:
            # Check if there is any account exists where is_active = False
            # if any, then get the first record and update details
            try:
                # update record in sub-account details
                sub_account = SubAccountDetails.objects.filter(is_active=False).first()
                # Generates Api key and Secret key for updating record
                master_api_object = MasterAccountAPI()
                sub_account_api_details = master_api_object.activate_sub_account(
                    subAccountId=sub_account.sub_account_id,
                    canTrade=True
                )
                sub_account.sub_account_api_key = sub_account_api_details["apiKey"]
                sub_account.sub_account_secret = sub_account_api_details["secretKey"]
                sub_account.is_active = True
                sub_account.save()

                # Insert record in UserSubaccount table (Composite table)
                today = date.today()
                start_date = today.strftime("%Y-%m-%d")

                user_sub_account_object = UserSubAccount(
                    start_date=start_date,
                    user_id_id=user_id,
                    sub_account_id=sub_account.sub_account_id,
                    sub_account_detail_id_id=sub_account.id,
                )
                user_sub_account_object.save()
                # return sub_account
            # if not found any row where is_active = False then, create new sub-account
            except Exception as e:
                try:
                    master_api_object = MasterAccountAPI()
                    sub_account_details = master_api_object.create_sub_account()
                    sub_account_api_details = master_api_object.activate_sub_account(
                        subAccountId=sub_account_details["subaccountId"], canTrade=True
                    )
                except Exception as e:
                    raise e

                # inserting sub-account data into  model
                sub_account = SubAccountDetails(
                    sub_account_id=sub_account_details["subaccountId"],
                    sub_account_email=sub_account_details["email"],
                    sub_account_api_key=sub_account_api_details["apiKey"],
                    sub_account_secret=sub_account_api_details["secretKey"],
                    can_trade=sub_account_api_details["canTrade"],
                    margin_trade=sub_account_api_details["marginTrade"],
                    futures_trade=sub_account_api_details["futuresTrade"],
                    is_active=True,
                )
                sub_account.save()
                today = date.today()
                start_date = today.strftime("%Y-%m-%d")
                user_sub_account_object = UserSubAccount(
                    sub_account_id=sub_account_details["subaccountId"],
                    start_date=start_date,
                    sub_account_detail_id_id=sub_account.id,
                    user_id_id=user_id
                )
                user_sub_account_object.save()
        sub_account_details = SubAccountDetails.objects.get(account_id=user_id)
        if not sub_account_details.is_uds_running:
            try:
                accountable_user_details = AccountableUser.objects.get(id=user_id)
                # deploy a job and start userdatastream
                task_function = "q_service.tasks.deploy_user_data_stream_job"
                task_name = (
                    f"deploy_user_data_stream_job_{sub_account_details.sub_account_id}"
                )
                hook_name = "q_service.hooks.post_deploy_user_data_stream_job"
                async_task(
                    task_function,
                    {
                        "api_key": sub_account_details.sub_account_api_key,
                        "api_secret": sub_account_details.sub_account_secret,
                        "sub_account_id:": sub_account_details.sub_account_id,
                        "account_id": accountable_user_details,
                    },
                    task_name=task_name,
                    hook=hook_name,
                )
            except Exception as e:
                raise e
        return sub_account


class SubAccountBalance(models.Model):
    # NOTE: sub account balance should be linked to a sub account and not the user, but maybe it's fine as the relation is one-to-one
    account_id = models.ForeignKey(AccountableUser, on_delete=CASCADE)
    asset = models.CharField(max_length=128)
    available = models.DecimalField(
        max_digits=50,
        decimal_places=25,
        default=0.0,
        validators=[MinValueValidator(0.0)],
    )
    total = models.DecimalField(
        max_digits=50,
        decimal_places=25,
        default=0.0,
        validators=[MinValueValidator(0.0)],
    )
    locked = models.DecimalField(
        max_digits=50,
        decimal_places=25,
        default=0.0,
        validators=[MinValueValidator(0.0)],
    )

    class Meta:
        db_table = "sub_account_balance"
        unique_together = ("account_id", "asset")

    # this function will update sub-account balance when asset deposit/withdrow from/to portfolio
    def update_balance_by_portfolio(
        self, available, deposit_to_portfolio=False, save=False
    ):
        if deposit_to_portfolio:
            self.available -= available
        else:
            self.available += available
        if save:
            self.save()

    # this function will update sub-account balance when asset deposit/withdrow from/to sub-account
    def update_balance_by_transaction(self, quantity, save=False):
        self.total += quantity
        self.available += quantity
        if save:
            self.save()


class SubAccountDepositHistory(models.Model):
    sub_account_id = models.BigIntegerField()
    amount = models.DecimalField(
        max_digits=50, decimal_places=25, validators=[MinValueValidator(0.0)]
    )
    coin = models.ForeignKey(
        Cryptocurrency, to_field="cryptocurrency", on_delete=DO_NOTHING
    )
    network = models.CharField(max_length=128)
    status = models.IntegerField(default=0)
    address = models.TextField()
    address_tag = models.TextField(null=True, blank=True)
    tx_id = models.TextField()
    deposited_at = models.DateTimeField()
    source_address = models.BigIntegerField()
    confirm_times = models.CharField(max_length=128)
    order_type = models.CharField(max_length=255, default="DEPOSIT")

    class Meta:
        db_table = "sub_account_deposit_history"


class TransferHistory(models.Model):
    from_id = models.CharField(max_length=250, blank=True)
    to_id = models.CharField(max_length=250, blank=True)
    asset = models.ForeignKey(
        Cryptocurrency, to_field="cryptocurrency", on_delete=DO_NOTHING
    )
    quantity = models.DecimalField(max_digits=50, decimal_places=25)
    time = models.DateTimeField()
    txn_id = models.BigIntegerField()
    status = models.CharField(max_length=128, null=False, blank=False)
    fake_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    def __str__(self) -> str:
        return f"{self.asset}-{self.quantity}"

    class Meta:
        db_table = "transfer_history"


class WithdrawHistory(models.Model):
    sub_account_id = models.CharField(max_length=500)
    order_id = models.CharField(max_length=500, null=False, blank=False, unique=True)
    amount = models.DecimalField(max_digits=50, decimal_places=25)
    transaction_fee = models.DecimalField(max_digits=50, decimal_places=25)
    coin = models.ForeignKey(
        Cryptocurrency, to_field="cryptocurrency", on_delete=DO_NOTHING
    )
    status = models.IntegerField()
    address = models.CharField(max_length=250, null=False, blank=False)
    tx_id = models.CharField(max_length=250, null=False, blank=False, unique=True)
    apply_time = models.DateTimeField(auto_now_add=False, editable=False)
    network = models.CharField(max_length=128, null=False, blank=False)
    transfer_type = models.IntegerField()
    info = models.CharField(max_length=500, null=True, blank=True)
    confirm_no = models.IntegerField(null=True, blank=True)
    order_type = models.CharField(max_length=255, default="WITHDRAW")

    def __str__(self) -> str:
        return self.order_id

    class Meta:
        db_table = "withdraw_history"


class UserSubAccount(models.Model):
    user_id = models.ForeignKey(AccountableUser, on_delete=DO_NOTHING)
    sub_account_detail_id = models.ForeignKey(SubAccountDetails, on_delete=DO_NOTHING)
    sub_account_id = models.TextField()
    start_date = models.DateField(null=True)
    end_date = models.DateField(null=True)
