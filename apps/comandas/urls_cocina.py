from django.urls import path
from . import views

urlpatterns = [
    path('kds/', views.kds_view, name='kds_view'),
    path('stock-platos/', views.stock_platos_view, name='stock_platos_view'),
]
