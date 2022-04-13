from rest_framework import status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from jwtauth.permissions import IsProfileCompleted, IsProfileCompleteOrReadOnly
from .serializer import (
    SubAccountBalanceSerializer,
    TransferHistorySerializer,
    WithdrawHistorySerializer,
    CoinSerializer,
)
from .models import SubAccountBalance, SubAccountDetails, WithdrawHistory, SubAccountDepositHistory
from rest_framework.views import APIView
from rest_framework.exceptions import APIException
from entropy.binance_api.generic import GenericAPI
from entropy.binance_api.master_account import MasterAccountAPI
import uuid
from .db_ops import DBOperations
from rest_framework.serializers import ValidationError
from itertools import chain
from django.shortcuts import get_object_or_404


class DepositAddress(APIView):
    permission_classes = [
        IsAuthenticated,
        IsProfileCompleteOrReadOnly,
    ]

    def post(self, request, *args, **kwargs):
        request_body_data = request.data
        logged_in_user = request.user.id
        # serializer for validating coin
        serializer = CoinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            # this method will check the sub-account exists or not.
            # if exists then return sub-account details else create new sub account
            sub_account_data = SubAccountDetails.get_or_create(user_id=logged_in_user)
            generic_api = GenericAPI(
                api_key=sub_account_data.sub_account_api_key,
                api_secret=sub_account_data.sub_account_secret,
            )
            # fetching coin address from binance.
            coin_address = generic_api.get_deposit_address(
                coin=request.data["coin"]
            )
            return Response(
                coin_address,
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            raise ValidationError("Please try after some time!")


class SubAccountBalanceView(APIView):
    permission_classes = [
        IsAuthenticated,
        IsProfileCompleteOrReadOnly,
    ]

    def get(self, request, *args, **kwargs):
        db_ops = DBOperations()

        try:
            # updating DB with latest balance
            db_ops.fetch_assets_balance_and_store_in_db(
                logged_in_user=request.user.id
            )
        except Exception as e:
            # dynamic message from serializer and e.message
            raise APIException("message")

        assets_balances = SubAccountBalance.objects.filter(
            account_id=request.user.id)
        serializer = SubAccountBalanceSerializer(
            assets_balances, many=True)
        return Response({"assets": serializer.data}, status=status.HTTP_200_OK)


class WithdrawView(APIView):
    permission_classes = [
        IsAuthenticated,
        IsProfileCompleteOrReadOnly
    ]

    def post(self, request, *args, **kwargs):
        
        # serializer for validating coin
        serializer = CoinSerializer(data={"coin" : request.data["asset"]})
        serializer.is_valid(raise_exception=True)
        
        db_ops = DBOperations()
        
        # update balance
        db_ops.fetch_assets_balance_and_store_in_db(request.user.id)

        updated_balance = get_object_or_404(SubAccountBalance,account_id=request.user.id, asset=request.data["asset"])
    
        master_api_obj = MasterAccountAPI()
        sub_account_data = SubAccountDetails.get_or_create(user_id=request.user.id)

        if updated_balance.available >= float(request.data["amount"]):
            # generating random client transaction ID
            client_transaction_id = str(uuid.uuid4()).replace("-", "")
            
            try:
                # sub-to-master
                transfer_res = master_api_obj.transfer(
                    asset=request.data["asset"],
                    amount=request.data["amount"],
                    fromId=sub_account_data.sub_account_id,
                    clientTranId=client_transaction_id,
                )
            except Exception as e:
                raise APIException(e.message)

            updated_transfer_history = db_ops.update_transfer_history(
                client_transaction_td=transfer_res["clientTranId"]
            )

            if len(updated_transfer_history) == 1:
                updated_transfer_history[0]["fake_id"] = transfer_res["clientTranId"]

            # save the updated_transfer_history into table
            serializer = TransferHistorySerializer(
                data=updated_transfer_history, many=True
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()

            try:
                # master to wallet
                # NOTE: use this address for testing with LTC
                withdraw_response_id =  master_api_obj.withdraw(
                coin=request.data["asset"], address="LXAnLnBbsN5hjkebcSMB6ZdP8rUgUo9QqT", amount=request.data["amount"]
                )

                latest_withdraw_entry = db_ops.get_latest_withdraw(
                    withdraw_response_id=withdraw_response_id)
                
                # once matched perform entry in DB_table with one extra column called sub_account_id
                current_sub_account = SubAccountDetails.objects.get(
                    account_id=request.user.id)
                latest_withdraw_entry["sub_account_id"] = current_sub_account.sub_account_id
                serializer = WithdrawHistorySerializer(data=latest_withdraw_entry)
                serializer.is_valid(raise_exception=True)

                serializer.save()

                return Response(
                    {"message": "Withdraw request submitted"}, status=status.HTTP_200_OK
                )
            except Exception as e:
                try:
                    # transfer coins back to sub account from master account
                    master_api_obj.transfer(
                        asset=request.data["asset"], amount=request.data["amount"], toId=sub_account_data.sub_account_id
                    )

                    return Response(
                        {"message": "Coins are transfered back to the sub account"}, status=status.HTTP_200_OK
                    )
                except Exception as e:
                    # TODO: need to handle this case where witdraw request for master to wallet fails and we are unable to transfer coins back to sub account 
                    raise APIException("Withdraw request failed")
        else:
            raise ValidationError("Insufficient Balance")

        
class WalletHistoryView(APIView):
    permission_classes = [
        IsAuthenticated,
        IsProfileCompleteOrReadOnly,
    ]

    def get(self, request, *args, **kwargs):
        # get withdraw history
        withdraw_history = WithdrawHistory.objects.extra(
            select={"timestamp": "apply_time"}
        ).values("coin", "amount", "order_type", "timestamp", "status")

        # get deposit history
        deposit_history = SubAccountDepositHistory.objects.extra(
            select={"timestamp": "deposited_at"}
        ).values("coin", "amount", "order_type", "timestamp", "status")

        # combined/chained two querysets
        report = chain(withdraw_history, deposit_history)
        
        # sort data according to timestamp
        return Response(sorted(
            list(report), key=lambda x: x["timestamp"], reverse=True
        ))
